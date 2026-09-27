"""Discord client with message monitoring and moderation panel builder."""
import asyncio
import re
import time
from collections import defaultdict, deque

import discord
from discord.ext import commands

from bot import akemi, llm, moderation, messages, translator
from bot.database import get_db
from bot.views.mod_buttons import DynamicModButton, ModActionView, RaidActionView

# Очередь модерации: message-объекты проверяются в фоне ограниченным числом
# воркеров, чтобы LLM-вызовы не блокировали event loop Discord и не упирались
# в rate limits API (429).
MODERATION_WORKERS = 2
MODERATION_QUEUE_MAX = 512
UNBAN_CHECK_INTERVAL = 60  # сек — проверка истёкших временных банов по БД
WEEKLY_DIGEST_INTERVAL = 7 * 86400  # 7 суток — еженедельный авто-дайджест для Главной Администрации
NEW_ACCOUNT_HOURS = 48     # аккаунты младше 48 часов помечаются как подозрительные на твинк (п. 2.1)

RECENT_MESSAGES: dict = defaultdict(lambda: deque(maxlen=20))
# Панели по пользователям: (guild_id, user_id) -> (канал_панели, id_панели,
# violation_id, канал_нарушения, id_нарушения) — для пометки «наказано через Akemi».
RECENT_PANELS: dict = {}
# Анти-рейд (правило 2.6): очередь входов по серверам (guild_id -> deque[(ts, user_id, name)])
RECENT_JOINS: dict = defaultdict(lambda: deque(maxlen=50))
LAST_RAID_ALERT: dict = {}
# Кулдаун панелей по профилям/никнеймам: (guild_id, user_id, rule_id) -> timestamp
RECENT_PROFILE_ALERTS: dict = {}
# Прогресс сканирования каналов (последний обработанный message_id) — копится в памяти,
# периодически сбрасывается в БД. После рестарта используется для бэкфилла офлайн-сообщений.
SCAN_PROGRESS: dict = {}
SCAN_FLUSH_INTERVAL = 20  # сек
BACKFILL_LIMIT = 200      # сколько сообщений максимум проверять на канал после рестарта
BACKFILL_PANEL_CAP = 20   # максимум панелей на канал за один бэкфилл


def flush_scan_progress() -> None:
    """Сброс буфера SCAN_PROGRESS в БД (периодически и при закрытии)."""
    if not SCAN_PROGRESS:
        return
    db = get_db()
    for (gid, chid), mid in list(SCAN_PROGRESS.items()):
        try:
            db.update_scan_progress(gid, chid, mid)
        except Exception as e:
            print(f"[SCAN] flush {gid}/{chid}: {e}")
    SCAN_PROGRESS.clear()

# Системные цвета бота (бренд сервера): фиолетовый — система/внимание,
# зелёный — успех/наказание вынесено.
BRAND_PURPLE = 0x7F5AF0
BRAND_GREEN = 0x16BE78

# Секунды ожидания перед панелью — даём Akemi время наказать/удалить,
# чтобы PMX не создавал дубликат нарушения.
AKEMI_SETTLE = 5

# Экстренная панель: N нарушений за окно (сек) у одного пользователя → пинг роли модерации.
EMERGENCY_WINDOW = 600        # последние 10 минут
EMERGENCY_COUNT = 6           # от 6 нарушений за окно
BRAND_RED = 0xE53E3E

# Анти-рейд (правило 2.6): всплеск входов за короткое окно
RAID_WINDOW = 10              # сек
RAID_THRESHOLD = 6            # от 6 входов за 10 секунд
RAID_COOLDOWN = 60            # сек между алертами рейда на один сервер
PROFILE_ALERT_COOLDOWN = 300  # сек между панелями по одному и тому же профилю
SPLIT_MSG_WINDOW = 20         # сек для склейки разбитых по словам оскорблений/банвордов


class ModBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.messages = True
        intents.moderation = True
        super().__init__(
            command_prefix="!",
            intents=intents,
            status=discord.Status.dnd,
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="за порядком Macronux™",
            ),
        )
        self._mod_queue: asyncio.Queue = asyncio.Queue(maxsize=MODERATION_QUEUE_MAX)
        self._bg_tasks: list[asyncio.Task] = []

    async def setup_hook(self):
        get_db().init()
        # Логирование ошибок команд и кнопок, чтобы «тихих» отказов не было.
        @self.tree.error
        async def on_tree_error(interaction: discord.Interaction, error: Exception):
            import traceback
            print(f"[INTERACTION-ERROR] {interaction.command} от {interaction.user}: "
                  f"{type(error).__name__}: {error}")
            traceback.print_exc()
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        f"Ошибка: {type(error).__name__}. Подробности в логе.", ephemeral=True
                    )
            except discord.HTTPException:
                pass

        # Динамический обработчик кнопок для любых старых панелей без ограничения в 200 штук.
        self.add_dynamic_items(DynamicModButton)

        # Персистентные кнопки: восстановить обработчики недавних панелей после рестарта по БД.
        from bot.rules import build_suggest
        restored = 0
        for v in get_db().list_recent_violations(limit=200):
            if not v.get("message_id"):
                continue
            try:
                n = get_db().count_real_violations(v["guild_id"], v["user_id"])
                self.add_view(ModActionView(
                    v["guild_id"], v["user_id"], v["id"],
                    target_msg_id=v.get("message_id"),
                    channel_id=v.get("channel_id"),
                    suggest=build_suggest(v.get("rule_id"), n,
                                          v.get("severity") or "medium"),
                ))
                restored += 1
            except Exception as e:
                print(f"[VIEW] пропуск панели #{v['id']}: {e}")
        if restored:
            print(f"[VIEW] восстановлено панелей с кнопками: {restored}")
        await self.load_extension("bot.cogs.settings")
        await self.load_extension("bot.cogs.stats")
        await self.load_extension("bot.cogs.rules_cmd")
        await self.load_extension("bot.cogs.rescan")
        await self.load_extension("bot.cogs.reports")
        await self.tree.sync()
        for guild in self.guilds:
            try:
                await self.tree.sync(guild=guild)
            except discord.HTTPException as e:
                print(f"[SYNC] guild {guild.id}: {e}")
        # Фоновые задачи: сброс прогресса сканирования, авто-разбан, авто-дайджест, мост Pulse/Pterodactyl и воркеры очереди.
        self._bg_tasks.append(self.loop.create_task(self._scan_flush_loop()))
        self._bg_tasks.append(self.loop.create_task(self._unban_loop()))
        self._bg_tasks.append(self.loop.create_task(self._weekly_digest_loop()))
        try:
            from bot.pulse_bridge import PulseBridge
            self.pulse_bridge = PulseBridge(self)
            self._bg_tasks.append(self.loop.create_task(self.pulse_bridge.sync_loop()))
        except Exception as e:
            print(f"[PULSE] ошибка запуска моста: {e}")
        for i in range(MODERATION_WORKERS):
            self._bg_tasks.append(self.loop.create_task(self._moderation_worker(i)))

    async def _weekly_digest_loop(self):
        """Еженедельный авто-дайджест для Главной Администрации в канал модерации."""
        from bot.cogs.stats import build_digest_embed
        while True:
            await asyncio.sleep(WEEKLY_DIGEST_INTERVAL)
            db = get_db()
            for guild in list(self.guilds):
                try:
                    server = db.get_server(guild.id)
                    channel = _resolve_mod_channel(guild, server.get("mod_channel_id"))
                    if channel is None:
                        continue
                    stats = db.get_digest_stats(guild.id, days=7)
                    if stats.get("total_violations", 0) == 0:
                        continue
                    embed = build_digest_embed(guild, days=7)
                    await channel.send(embed=embed)
                except Exception as e:
                    print(f"[DIGEST] ошибка авто-дайджеста для {guild.id}: {e}")


    async def _scan_flush_loop(self):
        """Периодический сброс маркера сканирования каналов в БД."""
        while True:
            await asyncio.sleep(SCAN_FLUSH_INTERVAL)
            try:
                flush_scan_progress()
            except Exception as e:
                print(f"[SCAN] flush error: {e}")

    async def _process_expired_bans(self):
        """Разбанивает пользователей, у которых истёк срок временного бана в БД."""
        db = get_db()
        for row in db.list_expired_bans():
            pid = row["id"]
            gid = row["guild_id"]
            uid = row["user_id"]
            vid = row.get("violation_id")
            days = max(round((row.get("duration_seconds") or 86400) / 86400), 1)
            guild = self.get_guild(gid)
            if guild is None:
                continue
            try:
                await guild.unban(
                    discord.Object(id=uid),
                    reason=f"Авторазбан по истечении {days} дн. (Violation #{vid})",
                )
                print(f"[UNBAN] пользователь {uid} разбанен на сервере {gid} (punishment #{pid})")
            except discord.NotFound:
                # Пользователь уже был разбанен вручную
                pass
            except discord.Forbidden:
                print(f"[UNBAN] нет прав на разбан {uid} на сервере {gid}")
            except discord.HTTPException as e:
                print(f"[UNBAN] ошибка API при разбане {uid}: {e}")
                continue
            db.mark_punishment_status(pid, "unbanned")

    async def _unban_loop(self):
        """Фоновый цикл проверки истёкших временных банов."""
        while True:
            await asyncio.sleep(UNBAN_CHECK_INTERVAL)
            try:
                await self._process_expired_bans()
            except Exception as e:
                print(f"[UNBAN] loop error: {e}")

    async def _moderation_worker(self, worker_id: int):
        """Фоновый воркер очереди модерации сообщений."""
        while True:
            message, pre_detect = await self._mod_queue.get()
            try:
                await self._handle_message_moderation(message, pre_detect)
            except Exception as e:
                import traceback
                print(f"[WORKER-{worker_id}] ошибка обработки сообщения {getattr(message, 'id', None)}: {e}")
                traceback.print_exc()
            finally:
                self._mod_queue.task_done()

    async def _backfill_scan(self):
        """После рестарта: проверяем сообщения, пропущенные за время выключения.

        Точка продолжения — последний обработанный message_id на канал (scan_progress).
        Новые нарушения получают панели модерации, дубли не создаются.
        """
        db = get_db()
        start = time.time()
        checked = panels = 0
        for row in db.list_scan_progress():
            gid, chid, last_id = row["guild_id"], row["channel_id"], row["last_message_id"]
            guild = self.get_guild(gid)
            if guild is None:
                continue
            channel = guild.get_channel(chid)
            if channel is None:
                continue
            try:
                if not channel.permissions_for(guild.me).read_message_history:
                    continue
            except AttributeError:
                continue
            max_seen = last_id
            try:
                async for msg in channel.history(
                        after=discord.Object(last_id), limit=BACKFILL_LIMIT,
                        oldest_first=True):
                    max_seen = max(max_seen, msg.id)
                    if msg.author.bot:
                        continue
                    checked += 1
                    server = db.get_server(gid)
                    detect = await detect_violation(msg, server)
                    if detect is None:
                        continue
                    rule_id, method, severity, reason, translation = detect
                    if db.violation_for_message(msg.id):
                        continue  # панель уже была создана
                    target_lang = server.get("target_language") or "ru"
                    try:
                        await post_violation_panel(
                            msg, server, rule_id, method, severity, reason,
                            target_lang, bool(server.get("delete_message")),
                            server.get("mod_channel_id"), translation=translation)
                        panels += 1
                    except Exception as e:
                        print(f"[BACKFILL] панель msg {msg.id}: {e}")
                    if panels >= BACKFILL_PANEL_CAP:
                        print(f"[BACKFILL] #{channel.name}: лимит панелей ({BACKFILL_PANEL_CAP}), стоп")
                        break
            except discord.Forbidden:
                continue
            except discord.HTTPException as e:
                print(f"[BACKFILL] #{channel.name}: {e}")
            db.update_scan_progress(gid, chid, max_seen)
        print(f"[BACKFILL] готово: проверено {checked}, панелей {panels}, за "
              f"{time.time() - start:.1f}s")

    async def close(self):
        for task in self._bg_tasks:
            task.cancel()
        self._bg_tasks.clear()
        if getattr(self, "pulse_bridge", None) is not None:
            try:
                await self.pulse_bridge.stop()
            except Exception as e:
                print(f"[PULSE] close error: {e}")
        try:
            flush_scan_progress()
        except Exception as e:
            print(f"[SCAN] flush on close: {e}")
        try:
            await llm.aclose()
        except Exception as e:
            print(f"[LLM] close error: {e}")
        try:
            get_db().close()
        except Exception as e:
            print(f"[DB] close error: {e}")
        await super().close()

    async def on_ready(self):
        print(f"Бот запущен: {self.user} (id={self.user.id})")
        try:
            await self.change_presence(status=discord.Status.dnd)
        except Exception as e:
            print(f"[PRESENCE] не удалось выставить статус DND: {e}")
        await akemi.detect(self)
        if not getattr(self, "_backfill_done", False):
            self._backfill_done = True
            self.loop.create_task(self._backfill_scan())
            self.loop.create_task(self._process_expired_bans())
        for guild in self.guilds:
            try:
                sync_guild_discord_metadata(guild)
            except Exception as e:
                print(f"[META] ошибка синхронизации метаданных сервера {guild.id}: {e}")
            server = get_db().get_server(guild.id)
            print(f"  Сервер: {guild.name} (id={guild.id}), канал модерации: "
                  f"{server.get('mod_channel_id')}, язык: {server.get('target_language')}")
            for ch in guild.text_channels:
                perms = ch.permissions_for(guild.me)
                if perms.view_channel and perms.read_messages and perms.read_message_history:
                    print(f"    [виден] #{ch.name}")
                else:
                    print(f"    [НЕ виден] #{ch.name}")

    async def on_guild_join(self, guild: discord.Guild):
        try:
            sync_guild_discord_metadata(guild)
        except Exception as e:
            print(f"[META] on_guild_join {guild.id}: {e}")

    async def on_guild_update(self, before: discord.Guild, after: discord.Guild):
        try:
            sync_guild_discord_metadata(after)
        except Exception as e:
            print(f"[META] on_guild_update {after.id}: {e}")

    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        if getattr(channel, "guild", None):
            try:
                sync_guild_discord_metadata(channel.guild)
            except Exception:
                pass

    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
        if getattr(after, "guild", None) and getattr(before, "name", None) != getattr(after, "name", None):
            try:
                sync_guild_discord_metadata(after.guild)
            except Exception:
                pass

    async def on_guild_role_create(self, role: discord.Role):
        if getattr(role, "guild", None):
            try:
                sync_guild_discord_metadata(role.guild)
            except Exception:
                pass

    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        if getattr(after, "guild", None) and getattr(before, "name", None) != getattr(after, "name", None):
            try:
                sync_guild_discord_metadata(after.guild)
            except Exception:
                pass

    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        # Сохраняем актуальный никнейм, аватарку и роли участника для веб-панели
        try:
            get_db().record_discord_member(message.guild.id, message.author)
        except Exception:
            pass
        # В голосовых каналах мониторим нарушения, но команды (слэш- и «!»-) игнорируем.
        is_voice = isinstance(message.channel, (discord.VoiceChannel, discord.StageChannel))
        content = message.content or ""
        if is_voice and (content.startswith("/") or content.startswith("!")):
            return
        print(f"[MSG] #{message.channel.name} | {message.author}: {message.content[:80]!r}")
        # record for flood detection
        record_message(message)

        # Channel restrictions (#команды only commands, #скриншоты only media):
        # обычный текст там просто удаляется БЕЗ страйков
        if await channel_restriction_delete(message):
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            SCAN_PROGRESS[(message.guild.id, message.channel.id)] = message.id
            return

        # Flood: удаление после 3-го одинакового сообщения, страйк после 5-го (§5.0)
        flood_n = flood_count(message)
        pre_detect = None
        if flood_n >= 5:
            pre_detect = ("3.6", "regex", "low", f"Флуд: {flood_n} одинаковых сообщений",
                          None)
        elif flood_n >= 3:
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            SCAN_PROGRESS[(message.guild.id, message.channel.id)] = message.id
            return

        if self._bg_tasks and not self._mod_queue.full():
            self._mod_queue.put_nowait((message, pre_detect))
        else:
            await self._handle_message_moderation(message, pre_detect)

    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        """Модерация отредактированных сообщений: защита от обхода через редактирование «Привет» -> скам/оскорбление."""
        if after.author.bot or not after.guild:
            return
        try:
            get_db().record_discord_member(after.guild.id, after.author)
        except Exception:
            pass
        before_text = (before.content or "").strip()
        after_text = (after.content or "").strip()
        if not after_text or before_text == after_text:
            return
        # Если по этому сообщению уже создана панель нарушения — дубль не создаём
        if get_db().violation_for_message(after.id):
            return
        is_voice = isinstance(after.channel, (discord.VoiceChannel, discord.StageChannel))
        if is_voice and (after_text.startswith("/") or after_text.startswith("!")):
            return
        print(f"[MSG-EDIT] #{after.channel.name} | {after.author}: {before_text[:40]!r} -> {after_text[:60]!r}")
        record_message(after)

        if await channel_restriction_delete(after):
            try:
                await after.delete()
            except discord.HTTPException:
                pass
            return

        if self._bg_tasks and not self._mod_queue.full():
            self._mod_queue.put_nowait((after, None))
        else:
            await self._handle_message_moderation(after, None)

    async def on_member_join(self, member: discord.Member):
        """Анти-рейд (п. 2.6) и проверка никнейма/профиля при входе (п. 2.2, 2.3, 3.7)."""
        if member.bot or not member.guild:
            return
        try:
            get_db().record_discord_member(member.guild.id, member)
        except Exception:
            pass
        now = time.time()
        gid = member.guild.id
        joins = RECENT_JOINS[gid]
        joins.append((now, member.id, f"{member.display_name} ({member})"))
        while joins and now - joins[0][0] > RAID_WINDOW:
            joins.popleft()

        if len(joins) >= RAID_THRESHOLD and now - LAST_RAID_ALERT.get(gid, 0) >= RAID_COOLDOWN:
            LAST_RAID_ALERT[gid] = now
            raiders = list(joins)
            try:
                await post_raid_alert_panel(member.guild, member, raiders)
            except Exception as e:
                print(f"[RAID] ошибка отправки алерта: {e}")

        await self._check_member_profile_violation(member)

    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """Проверка изменений никнейма и кастомного статуса участника (п. 2.2, 2.3, 3.7)."""
        if after.bot or not after.guild:
            return
        before_dn = (getattr(before, "display_name", "") or "").strip()
        after_dn = (getattr(after, "display_name", "") or "").strip()
        before_gn = (getattr(before, "global_name", "") or "").strip()
        after_gn = (getattr(after, "global_name", "") or "").strip()
        before_av = str(getattr(getattr(before, "display_avatar", None), "url", ""))
        after_av = str(getattr(getattr(after, "display_avatar", None), "url", ""))
        before_st = moderation._extract_member_status(before)
        after_st = moderation._extract_member_status(after)
        if before_dn != after_dn or before_gn != after_gn or before_av != after_av:
            try:
                get_db().record_discord_member(after.guild.id, after)
            except Exception:
                pass
        if before_dn == after_dn and before_gn == after_gn and before_st == after_st:
            return
        await self._check_member_profile_violation(after)

    async def _check_member_profile_violation(self, member: discord.Member):
        """Проверяет профиль участника на нарушение правил 2.2, 2.3, 3.7,
        автоматически сбрасывает запрещённый никнейм и публикует панель модерации."""
        db = get_db()
        server = db.get_server(member.guild.id)
        guild_banwords = db.list_banwords(member.guild.id)
        res = moderation.check_member_profile(
            member, guild_banwords, mod_role_id=server.get("mod_role_id")
        )
        if not res or not res.detected():
            return
        now = time.time()
        alert_key = (member.guild.id, member.id, res.rule_id)
        if now - RECENT_PROFILE_ALERTS.get(alert_key, 0) < PROFILE_ALERT_COOLDOWN:
            return
        RECENT_PROFILE_ALERTS[alert_key] = now

        old_display = getattr(member, "display_name", "") or str(member)
        auto_renamed = None
        # Если нарушение в отображаемом имени/никнейме — сразу переименовываем нарушителя
        if "статусе" not in (res.reason or "") and hasattr(member, "edit"):
            fallback_nick = f"Участник #{str(member.id)[-4:]}"
            try:
                await member.edit(
                    nick=fallback_nick,
                    reason=f"Авто-сброс запрещённого никнейма (п. {res.rule_id}): {old_display[:40]}",
                )
                auto_renamed = fallback_nick
            except (discord.Forbidden, discord.HTTPException):
                auto_renamed = None

        print(f"[PROFILE-VIOLATION] {res.rule_id} ({res.severity}) у {member}: {res.reason}")
        try:
            await post_profile_violation_panel(
                member, server, res.rule_id, res.method, res.severity, res.reason,
                old_display_name=old_display, auto_renamed=auto_renamed,
            )
        except Exception as e:
            print(f"[PROFILE-PANEL] ошибка: {e}")

    async def _handle_message_moderation(self, message: discord.Message,
                                         pre_detect: tuple | None = None):
        db = get_db()
        server = db.get_server(message.guild.id)
        mod_channel = server.get("mod_channel_id")

        detect = pre_detect if pre_detect is not None else await detect_violation(message, server)
        if detect is None:
            SCAN_PROGRESS[(message.guild.id, message.channel.id)] = message.id
            return

        rule_id, method, severity, reason, translation = detect
        print(f"[VIOLATION] {rule_id} ({method}/{severity}) от {message.author}: {message.content[:60]!r}")

        # Мгновенный карантин: скам-ссылки/инвайты (3.3) и деанон/личные данные (3.4)
        # удаляются из чата СРАЗУ ЖЕ (даже до ожидания Akemi и даже если delete_message=False),
        # чтобы никто из участников не перешёл по фишингу и не увидел чужие личные данные.
        force_quarantine = rule_id in ("3.3", "3.4")
        if force_quarantine:
            try:
                await message.delete()
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                pass

        # Совместимость с Akemi: если он уже наказал/удалил это нарушение — не дублируем.
        if akemi.resolved_id() is not None:
            await asyncio.sleep(AKEMI_SETTLE)
            if await akemi.handled_by_akemi(message.guild, message.author, message.channel.id):
                print(f"[Akemi] уже обработал {rule_id} от {message.author} — дубль не создаём")
                SCAN_PROGRESS[(message.guild.id, message.channel.id)] = message.id
                return
        # Decision on auto-delete
        delete_msg = bool(server.get("delete_message")) or force_quarantine
        target_lang = server.get("target_language") or "ru"

        # Build moderation panel (перевод уже готов из объединённого LLM-вызова)
        try:
            await post_violation_panel(
                message, server, rule_id, method, severity, reason,
                target_lang, delete_msg, mod_channel, translation=translation)
        except Exception as e:
            import traceback
            print(f"[ERROR] обработка нарушений: {type(e).__name__}: {e}")
            traceback.print_exc()

        if delete_msg and not force_quarantine:
            try:
                await message.delete()
            except (discord.Forbidden, discord.NotFound):
                pass
        SCAN_PROGRESS[(message.guild.id, message.channel.id)] = message.id


    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry):
        akemi.cache_from_entry(entry)
        uid = akemi.punished_user(entry)
        if uid is not None:
            # Модератор наказал через Akemi — помечаем последнюю панель этого
            # пользователя как решённую и гасим её кнопки.
            await mark_panel_handled_by_akemi(self, entry.guild.id, uid)

    async def on_error(self, event, *args, **kwargs):
        import traceback
        print(f"[ERROR в событии {event}]")
        traceback.print_exc()


def build_message_context(message: discord.Message) -> tuple[str, str]:
    """Собирает контекст сообщения для защиты от дробления («ты» / «полный» / «дегенерат») и анализа Reply.

    Возвращает:
      - combined_recent_text: склеенные последние реплики этого же автора за SPLIT_MSG_WINDOW сек;
      - llm_context: текстовое описание Reply + предыдущих реплик автора для LLM.
    """
    now = message.created_at.timestamp() if getattr(message, "created_at", None) else time.time()
    key = (message.guild.id, message.author.id)
    recent = list(RECENT_MESSAGES.get(key, ()))
    ch_id = getattr(message.channel, "id", None)
    msg_id = getattr(message, "id", None)
    cur_text = (message.content or "").strip()

    # Собираем предыдущие реплики этого автора в этом же канале
    prev_short = []
    prev_for_llm = []
    for rec in recent:
        if rec.get("channel") != ch_id or rec.get("id") == msg_id:
            continue
        raw_txt = (rec.get("raw") or rec.get("content") or "").strip()
        if not raw_txt:
            continue
        dt = abs(now - (rec.get("time") or now))
        if dt <= SPLIT_MSG_WINDOW:
            prev_short.append(raw_txt)
        if dt <= 60:
            prev_for_llm.append(raw_txt)

    combined_parts = prev_short[-3:] + ([cur_text] if cur_text else [])
    combined_recent_text = " ".join(combined_parts).strip() if len(combined_parts) > 1 else ""

    ctx_lines = []
    # Контекст Reply (ответа на сообщение другого участника)
    ref = getattr(message, "reference", None)
    if ref is not None:
        ref_msg = getattr(ref, "resolved", None) or getattr(ref, "cached_message", None)
        ref_content = getattr(ref_msg, "content", None)
        if ref_content and isinstance(ref_content, str) and ref_content.strip():
            ref_author = getattr(getattr(ref_msg, "author", None), "display_name", "участник")
            ctx_lines.append(f"Ответ (Reply) на сообщение от {ref_author}: «{ref_content.strip()[:240]}»")

    if prev_for_llm:
        joined_prev = " -> ".join(f"«{m[:120]}»" for m in prev_for_llm[-3:])
        ctx_lines.append(f"Предыдущие сообщения этого же автора подряд: {joined_prev}")

    return combined_recent_text, "\n".join(ctx_lines)


async def detect_violation(message: discord.Message, server) -> tuple | None:
    """Run regex then LLM analysis (with Reply + split-message dialogue context).
    Returns (rule_id, method, severity, reason, translation) or None.
    Rules excluded for the channel (rule_exceptions) are skipped."""
    text = message.content or ""

    if not text.strip():
        # attachments-only messages are not violations (media is fine in normal channels)
        return None

    db = get_db()
    guild_banwords = db.list_banwords(message.guild.id)
    excluded = set(db.list_rule_exceptions(message.guild.id, message.channel.id))
    if "all" in excluded:
        return None

    def allowed(rule_id: str) -> bool:
        return rule_id not in excluded

    # 1. Regex layer на само сообщение
    regex_res = moderation.regex_check(text, guild_banwords)
    if regex_res and regex_res.detected() and allowed(regex_res.rule_id):
        return (regex_res.rule_id, regex_res.method, regex_res.severity,
                regex_res.reason, None)

    # 2. Защита от дробления («ты» / «полный» / «дегенерат») — проверяем склейку последних коротких реплик автора
    combined_recent_text, llm_context = build_message_context(message)
    if combined_recent_text and combined_recent_text.lower() != text.strip().lower():
        split_res = moderation.regex_check(combined_recent_text, guild_banwords)
        if split_res and split_res.detected() and allowed(split_res.rule_id):
            return (
                split_res.rule_id,
                split_res.method,
                split_res.severity,
                f"{split_res.reason} (разбито на сообщения: «{combined_recent_text[:120]}»)",
                None,
            )

    # 3. LLM layer (only if enabled): один вызов = вердикт + перевод (с учётом контекста диалога и Few-Shot dismiss)
    if server.get("llm_enabled"):
        llm_res, translation = await moderation.llm_check_with_translation(
            text,
            guild_banwords,
            target_lang=server.get("target_language") or "ru",
            context=llm_context or None,
            guild_id=message.guild.id,
        )
        if llm_res and llm_res.detected() and allowed(llm_res.rule_id):
            return (llm_res.rule_id, llm_res.method, llm_res.severity,
                    llm_res.reason, translation)

    return None


async def channel_restriction_delete(message: discord.Message) -> bool:
    """Delete messages that violate channel restrictions (#команды, #скриншоты).
    Системный канал бота (#system) и голосовые каналы ограничениям не подлежат."""
    channel_name = message.channel.name.lower() if message.channel else ""
    if "system" in channel_name or "систем" in channel_name:
        return False
    if isinstance(message.channel, (discord.VoiceChannel, discord.StageChannel)):
        return False
    if "команд" in channel_name or "commands" in channel_name or "bot" in channel_name:
        # Разрешены только команды бота. Slash-команды Discord НЕ приходят в
        # on_message (их обрабатывает interaction), поэтому проверять content
        # на "/" нельзя — иначе любое текстовое «не-командное» сообщение удалится
        # безусловно. Пропускаем только префикс-команды бота ("!").
        if not message.content.startswith("!"):
            return True
    if "скриншот" in channel_name or "screenshot" in channel_name or "media" in channel_name:
        if not message.attachments:
            return True
    return False


def record_message(message: discord.Message):
    key = (message.guild.id, message.author.id)
    raw_content = (message.content or "").strip()
    RECENT_MESSAGES[key].append({
        "id": getattr(message, "id", None),
        "content": raw_content.lower(),
        "raw": raw_content,
        "channel": message.channel.id,
        "time": message.created_at.timestamp() if getattr(message, "created_at", None) else time.time(),
    })


FLOOD_WINDOW = 5  # seconds
FLOOD_DELETE = 3  # delete after 3rd identical (§5.0)
FLOOD_STRIKE = 5  # strike after 5th identical (§5.0)


def flood_count(message: discord.Message) -> int:
    """Number of identical messages (including current) within the window."""
    key = (message.guild.id, message.author.id)
    channel_name = message.channel.name.lower()
    if ("command" in channel_name or "bot" in channel_name
            or "system" in channel_name or "систем" in channel_name):
        return 0
    if not message.content.strip():
        return 0
    recent = RECENT_MESSAGES[key]
    if not recent:
        return 0
    content = message.content.strip().lower()
    now = message.created_at.timestamp()
    count = 0
    for rec in recent:
        if rec["content"] == content and now - rec["time"] <= FLOOD_WINDOW:
            count += 1
    return count



def _account_age_hours(user) -> float | None:
    """Возвращает возраст Discord-аккаунта в часах (или None, если created_at недоступен)."""
    created_at = getattr(user, "created_at", None)
    if created_at is None:
        return None
    try:
        now_ts = time.time()
        return max(0.0, (now_ts - created_at.timestamp()) / 3600.0)
    except Exception:
        return None


def sync_guild_discord_metadata(guild: discord.Guild) -> None:
    """Синхронизирует метаданные сервера (название, иконку, онлайн/участников, список каналов, ролей и профили) с БД."""
    if guild is None:
        return
    import json as _json
    db = get_db()
    server = db.get_server(guild.id)

    icon_url = str(guild.icon.url) if getattr(guild, "icon", None) else None
    member_count = int(getattr(guild, "member_count", None) or len(getattr(guild, "members", ())) or 0)
    mod_ch_obj = _resolve_mod_channel(guild, server.get("mod_channel_id"))
    mod_ch_name = getattr(mod_ch_obj, "name", None) if mod_ch_obj else None
    mod_role_id = server.get("mod_role_id")
    mod_role_obj = guild.get_role(mod_role_id) if mod_role_id else None
    mod_role_name = getattr(mod_role_obj, "name", None) if mod_role_obj else None

    db.upsert_guild_metadata(
        guild_id=guild.id,
        guild_name=guild.name,
        guild_icon_url=icon_url,
        member_count=member_count,
        mod_channel_name=mod_ch_name,
        mod_role_name=mod_role_name,
    )

    channels_list = []
    for ch in getattr(guild, "channels", ()):
        if isinstance(ch, discord.CategoryChannel):
            continue
        ch_type = "text"
        if isinstance(ch, discord.VoiceChannel):
            ch_type = "voice"
        elif isinstance(ch, discord.StageChannel):
            ch_type = "stage"
        elif isinstance(ch, discord.ForumChannel):
            ch_type = "forum"
        cat_name = getattr(getattr(ch, "category", None), "name", None)
        channels_list.append({
            "channel_id": ch.id,
            "name": ch.name,
            "type": ch_type,
            "category_name": cat_name,
            "position": getattr(ch, "position", 0),
        })

    roles_list = []
    for rl in getattr(guild, "roles", ()):
        if getattr(rl, "is_default", lambda: False)():
            continue
        perms = getattr(rl, "permissions", None)
        is_staff = 1 if perms and (
            perms.administrator or perms.moderate_members or perms.manage_messages or perms.ban_members or perms.kick_members
        ) else 0
        col_str = str(getattr(rl, "color", ""))
        roles_list.append({
            "role_id": rl.id,
            "name": rl.name,
            "color": col_str if col_str and col_str != "#000000" else None,
            "position": getattr(rl, "position", 0),
            "is_staff": is_staff,
        })

    db.sync_guild_channels_and_roles(guild.id, channels_list, roles_list)

    # Обогащаем профили всех участников, которые уже фигурируют в нарушениях или наказаниях (а также модераторов)
    important_uids: set[int] = set()
    try:
        with db._session() as conn:
            for r in conn.execute(
                "SELECT DISTINCT user_id FROM violations WHERE guild_id = ? UNION SELECT DISTINCT user_id FROM punishments WHERE guild_id = ? UNION SELECT DISTINCT moderator_id FROM punishments WHERE guild_id = ?",
                (guild.id, guild.id, guild.id),
            ).fetchall():
                if r[0]:
                    important_uids.add(int(r[0]))
    except Exception:
        pass

    recorded = 0
    for uid in important_uids:
        m = guild.get_member(uid)
        if m is not None:
            db.record_discord_member(guild.id, m)
            recorded += 1

    # Также сохраняем до 300 активных участников/модераторов из кэша гильдии
    for m in getattr(guild, "members", ()):
        if m.bot:
            continue
        if m.id in important_uids:
            continue
        perms = getattr(m, "guild_permissions", None)
        is_mod = perms and (perms.administrator or perms.moderate_members or perms.manage_messages or perms.ban_members)
        if is_mod or recorded < 300:
            db.record_discord_member(guild.id, m)
            recorded += 1


async def post_violation_panel(message, server, rule_id, method, severity, reason,
                               target_lang, delete_msg, mod_channel_id, translation=None):
    import json as _json
    db = get_db()
    guild = message.guild

    # Ensure mod channel exists
    channel = _resolve_mod_channel(guild, mod_channel_id)
    if channel is None:
        print(f"[PANEL] ВНИМАНИЕ: канал модерации не найден "
              f"(mod_channel_id={mod_channel_id}, system={guild.system_channel})")
        return
    print(f"[PANEL] шлю в #{channel.name} (id={channel.id}), правa: "
          f"send={channel.permissions_for(guild.me).send_messages}")

    lang = target_lang or "ru"

    # Детектор возраста аккаунта (Account Age < 48 ч. -> подозрение на твинк п. 2.1)
    age_hours = _account_age_hours(message.author)
    is_new_account = age_hours is not None and age_hours < NEW_ACCOUNT_HOURS
    if is_new_account and severity == "medium":
        severity = "high"
    elif is_new_account and severity == "low":
        severity = "medium"

    if translation:
        translated = translation
        detected_lang = translator.detect_language(message.content)
        # перевод уже был сделан в объединённом LLM-вызове при детекте — второй вызов не нужен
    else:
        translated = await translator.translate(message.content, target_lang)
        detected_lang = translator.detect_language(message.content)

    # Сохраняем актуальный профиль автора и метаданные сообщения
    user_meta = db.record_discord_member(guild.id, message.author)
    ch_name = getattr(message.channel, "name", None)
    jump_url = getattr(message, "jump_url", None)
    attachments_list = [
        {"url": str(a.url), "filename": getattr(a, "filename", ""), "content_type": getattr(a, "content_type", None)}
        for a in getattr(message, "attachments", ()) or ()
        if getattr(a, "url", None)
    ]
    attachments_json = _json.dumps(attachments_list, ensure_ascii=False) if attachments_list else None

    violation_id = db.add_violation(
        guild.id, message.author.id,
        guild_name=guild.name,
        user_name=user_meta.get("username"),
        user_display_name=user_meta.get("display_name"),
        user_avatar_url=user_meta.get("avatar_url"),
        rule_id=rule_id,
        severity=severity,
        method=method,
        reason=reason,
        message_snapshot=message.content,
        original_text=message.content,
        translated_text=translated,
        detected_language=detected_lang,
        channel_id=message.channel.id,
        channel_name=ch_name,
        message_id=message.id,
        jump_url=jump_url,
        attachments_json=attachments_json,
    )

    # Число нарушений (реальные, без «пропущенных») — для эскалации наказания
    violations_n = db.count_real_violations(guild.id, message.author.id)
    # Экстренный сценарий: гигантское число нарушений за короткое окно — пишем роль модерации
    emergency = db.count_recent_violations(
        guild.id, message.author.id, EMERGENCY_WINDOW) >= EMERGENCY_COUNT

    from bot.rules import (RULES, build_suggest, action_label, fmt_duration,
                           severity_emoji, severity_label)
    rule_info = RULES.get(rule_id)
    rule_title = rule_info["title"] if rule_info else rule_id
    suggest = build_suggest(rule_id, violations_n, severity)
    primary_action, primary_secs = suggest["action"], suggest.get("primary_secs")
    recommendation = (
        f"**{action_label(primary_action)} {fmt_duration(primary_secs)}**"
        if (primary_secs or primary_action == "ban") else f"**{action_label(primary_action)}**"
    )

    embed = discord.Embed(
        title=f"🚨{'🚨' if emergency else ''} Нарушение №{violation_id}",
        color=discord.Color(BRAND_RED if emergency else BRAND_PURPLE),
    )
    embed.set_thumbnail(url=message.author.display_avatar.url)
    embed.set_author(name=f"{message.author.display_name} ({message.author})",
                     icon_url=message.author.display_avatar.url)

    # Участник
    member_val = f"{message.author.mention} · <#{message.channel.id}>"
    if rule_id in ("3.3", "3.4"):
        member_val += "\n🛡️ *Сообщение мгновенно удалено карантином*"
    embed.add_field(
        name=messages.t(lang, "member_field"),
        value=member_val,
        inline=False,
    )

    if is_new_account:
        embed.add_field(
            name="⚠️ Новый аккаунт (подозрение на твинк п. 2.1)",
            value=f"Аккаунт создан **{max(1, int(age_hours))} ч. назад** (< {NEW_ACCOUNT_HOURS} ч.).",
            inline=False,
        )

    # Сообщение + перевод одной строкой (лимит поля эмбеда — 1024 символа)
    msg_val = snip(message.content or "(вложения)", limit=900)
    if translated and translated != message.content:
        budget = 1024 - len(msg_val) - 5  # "\n> 🌐 " + запасы
        if budget > 12:
            msg_val += f"\n> 🌐 {snip(translated, limit=budget)}"
    embed.add_field(name=messages.t(lang, "original"), value=msg_val, inline=False)

    # Правило + рекомендация по числу нарушений
    embed.add_field(
        name=messages.t(lang, "by_rule", rule=rule_id, title=rule_title),
        value=f"{messages.t(lang, 'recommendation')} {recommendation}\n"
              f"{messages.t(lang, 'violations_count', n=violations_n)} · "
              f"{severity_emoji(severity)} {severity_label(severity)}"
              + (f"\n{reason}" if reason else ""),
        inline=False,
    )

    embed.set_footer(
        text=f"#{message.channel.name} · {messages.t(lang, 'footer_stamp', violation_id=violation_id)}"
    )

    if emergency:
        emb_n = db.count_recent_violations(guild.id, message.author.id, EMERGENCY_WINDOW)
        embed.add_field(
            name="🚨 Экстренно: рецидив",
            value=f"**{emb_n} нарушений за 10 минут** — нужен мгновенный ответ модерации.",
            inline=False,
        )

    # Пинг роли модерации (экстренный режим): упоминание отправим в content панели.
    ping_content = None
    mod_role = guild.get_role(server.get("mod_role_id")) if server.get("mod_role_id") else None
    if emergency and mod_role is not None:
        ping_content = f"🚨 {mod_role.mention} — экстренное нарушение №{violation_id}"

    view = ModActionView(guild.id, message.author.id, violation_id,
                         target_msg_id=message.id, channel_id=message.channel.id,
                         lang=lang, suggest=suggest)

    try:
        sent = await channel.send(content=ping_content, embed=embed, view=view)
        RECENT_PANELS[(guild.id, message.author.id)] = (
            channel.id, sent.id, violation_id, message.channel.id, message.id,
        )
        print(f"[PANEL] отправлено ok (id={channel.id})")
    except discord.Forbidden:
        print(f"[PANEL] НЕТ ПРАВ отправлять в #{channel.name} — включите Send Messages для бота")
    except Exception as e:
        import traceback
        print(f"[PANEL] ошибка отправки: {type(e).__name__}: {e}")
        traceback.print_exc()

    # Телеметрия: зафиксированный ботом кейс нарушения (входные данные для обучения).
    from bot.telemetry import log_event
    try:
        log_event(
            "violation_detected",
            guild_id=guild.id,
            guild_name=guild.name,
            channel_id=message.channel.id,
            channel_name=ch_name,
            user_id=message.author.id,
            user_name=user_meta.get("username"),
            user_display_name=user_meta.get("display_name"),
            user_avatar_url=user_meta.get("avatar_url"),
            violation_id=violation_id,
            rule_id=rule_id,
            severity=severity,
            method=method,
            reason=reason,
            detected_language=detected_lang,
            text=message.content or None,
            translated_text=translated or None,
            jump_url=jump_url,
            emergency=emergency,
        )
    except Exception as e:
        print(f"[TELEMETRY] violation_detected: {e}")


def snip(text: str, limit: int = 1024) -> str:
    if len(text) <= limit:
        return text or "—"
    return text[: limit - 3] + "..."


async def mark_panel_handled_by_akemi(bot, guild_id: int, user_id: int):
    """Модератор наказал через Akemi — дописываем в последнюю панель PMX
    пометку «Наказание вынесено (Akemi)» и отключаем её кнопки."""
    info = RECENT_PANELS.get((guild_id, user_id))
    if not info:
        return
    panel_ch_id, panel_msg_id, violation_id, orig_ch_id, orig_msg_id = info
    channel = bot.get_channel(panel_ch_id)
    if channel is None:
        return
    try:
        panel = await channel.fetch_message(panel_msg_id)
        embed = discord.Embed.from_dict(panel.embeds[0].to_dict())
    except (discord.NotFound, discord.Forbidden, discord.HTTPException, IndexError):
        return
    embed.add_field(
        name="⚖ Наказание вынесено",
        value="**Через Akemi** (модератор воспользовался Akemi)\nКнопки PMX больше не требуются.",
        inline=False,
    )
    embed.color = discord.Color(BRAND_GREEN)
    view = ModActionView(guild_id, user_id, violation_id,
                         target_msg_id=orig_msg_id, channel_id=orig_ch_id)
    for child in view.children:
        if isinstance(child, discord.ui.Button):
            child.disabled = True
    try:
        await panel.edit(embed=embed, view=view)
        print(f"[Akemi] панель #{violation_id} помечена как решённая через Akemi")
    except (discord.HTTPException, discord.NotFound):
        pass


def _resolve_mod_channel(guild: discord.Guild, mod_channel_id: int | None):
    """Находит канал модерации на сервере по настройке или по названию."""
    channel = guild.get_channel(mod_channel_id) if mod_channel_id else None
    if channel is None:
        for ch in getattr(guild, "text_channels", None) or ():
            low_name = ch.name.lower()
            if "mod" in low_name or "мод" in low_name or "модер" in low_name:
                channel = ch
                break
    if channel is None:
        channel = getattr(guild, "system_channel", None)
    return channel


async def post_profile_violation_panel(
    member: discord.Member,
    server: dict,
    rule_id: str,
    method: str,
    severity: str,
    reason: str,
    old_display_name: str = None,
    auto_renamed: str = None,
):
    """Отправляет панель модерации по нарушению в профиле/никнейме/статусе (правила 2.2, 2.3, 3.7)."""
    db = get_db()
    guild = member.guild
    channel = _resolve_mod_channel(guild, server.get("mod_channel_id"))
    if channel is None:
        return

    user_meta = db.record_discord_member(guild.id, member)
    lang = server.get("target_language") or "ru"
    shown_name = old_display_name or member.display_name
    status_text = moderation._extract_member_status(member)
    profile_snapshot = f"Никнейм: {shown_name} ({member})"
    if status_text:
        profile_snapshot += f" | Статус: {status_text}"
    if auto_renamed:
        profile_snapshot += f"\n✅ Авто-переименован ботом в: «{auto_renamed}»"

    violation_id = db.add_violation(
        guild.id, member.id,
        guild_name=guild.name,
        user_name=user_meta.get("username"),
        user_display_name=shown_name,
        user_avatar_url=user_meta.get("avatar_url"),
        rule_id=rule_id,
        severity=severity,
        method=method,
        reason=reason,
        message_snapshot=profile_snapshot,
        original_text=profile_snapshot,
        translated_text=None,
        detected_language="ru",
        channel_id=channel.id,
        channel_name=getattr(channel, "name", None),
        message_id=0,
    )
    violations_n = db.count_real_violations(guild.id, member.id)
    from bot.rules import (RULES, build_suggest, action_label, fmt_duration,
                           severity_emoji, severity_label)
    rule_info = RULES.get(rule_id)
    rule_title = rule_info["title"] if rule_info else rule_id
    suggest = build_suggest(rule_id, violations_n, severity)
    primary_action, primary_secs = suggest["action"], suggest.get("primary_secs")
    recommendation = (
        f"**{action_label(primary_action)} {fmt_duration(primary_secs)}**"
        if (primary_secs or primary_action == "ban") else f"**{action_label(primary_action)}**"
    )

    embed = discord.Embed(
        title=f"👤 Нарушение в профиле №{violation_id}",
        color=discord.Color(BRAND_RED if severity == "high" else BRAND_PURPLE),
    )
    if getattr(member, "display_avatar", None):
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_author(name=f"{shown_name} ({member})", icon_url=member.display_avatar.url)
    else:
        embed.set_author(name=f"{shown_name} ({member})")

    embed.add_field(
        name=messages.t(lang, "member_field"),
        value=f"{member.mention} · Профиль участника",
        inline=False,
    )
    age_hours = _account_age_hours(member)
    if age_hours is not None and age_hours < NEW_ACCOUNT_HOURS:
        embed.add_field(
            name="⚠️ Новый аккаунт (подозрение на твинк п. 2.1)",
            value=f"Аккаунт создан **{max(1, int(age_hours))} ч. назад** (< {NEW_ACCOUNT_HOURS} ч.).",
            inline=False,
        )
    embed.add_field(
        name="Профиль / Статус",
        value=snip(profile_snapshot, limit=900),
        inline=False,
    )
    embed.add_field(
        name=messages.t(lang, "by_rule", rule=rule_id, title=rule_title),
        value=f"{messages.t(lang, 'recommendation')} {recommendation}\n"
              f"{messages.t(lang, 'violations_count', n=violations_n)} · "
              f"{severity_emoji(severity)} {severity_label(severity)}"
              + (f"\n{reason}" if reason else ""),
        inline=False,
    )
    embed.set_footer(text=f"Профиль · {messages.t(lang, 'footer_stamp', violation_id=violation_id)}")

    view = ModActionView(
        guild.id, member.id, violation_id,
        target_msg_id=None, channel_id=channel.id,
        lang=lang, suggest=suggest,
        show_nick_btn=True,
    )
    try:
        await channel.send(embed=embed, view=view)
    except discord.HTTPException as e:
        print(f"[PROFILE-PANEL] ошибка отправки: {e}")


async def post_raid_alert_panel(guild: discord.Guild, trigger_member: discord.Member,
                                raiders: list[tuple[float, int, str]]):
    """Отправляет экстренную панель Анти-Рейда (правило 2.6: Набеги и рейды) с боевыми кнопками и пингом."""
    db = get_db()
    server = db.get_server(guild.id)
    channel = _resolve_mod_channel(guild, server.get("mod_channel_id"))
    if channel is None:
        return

    user_meta = db.record_discord_member(guild.id, trigger_member)
    raider_lines = [f"• <@{uid}> (`{name}`)" for _, uid, name in raiders[-15:]]
    snapshot = f"Массовый вход ({len(raiders)} чел. за {RAID_WINDOW} сек.): " + ", ".join(
        name for _, _, name in raiders[-10:]
    )

    violation_id = db.add_violation(
        guild.id, trigger_member.id,
        guild_name=guild.name,
        user_name=user_meta.get("username"),
        user_display_name=user_meta.get("display_name"),
        user_avatar_url=user_meta.get("avatar_url"),
        rule_id="2.6",
        severity="high",
        method="raid_detector",
        reason=f"Всплеск входов: {len(raiders)} чел. за {RAID_WINDOW} сек.",
        message_snapshot=snapshot[:500],
        original_text=snapshot[:500],
        translated_text=None,
        detected_language="ru",
        channel_id=channel.id,
        channel_name=getattr(channel, "name", None),
        message_id=0,
    )
    from bot.rules import RULES
    rule_info = RULES.get("2.6", {"title": "Набеги и рейды"})

    embed = discord.Embed(
        title=f"🚨🚨 АНТИ-РЕЙД: Подозрение на набег (№{violation_id})",
        description=(
            f"Зафиксирован резкий всплеск входов: **{len(raiders)} участников за {RAID_WINDOW} сек.**\n"
            f"По правилу **2.6 ({rule_info['title']})** за организацию и участие в рейдах предусмотрен **Перманентный бан**.\n"
            f"Используйте кнопки ниже для мгновенного бана всей волны или включения паузы инвайтов."
        ),
        color=discord.Color(BRAND_RED),
    )
    embed.add_field(
        name=f"Вошедшие аккаунты ({len(raiders)})",
        value=snip("\n".join(raider_lines), limit=1000),
        inline=False,
    )
    embed.set_footer(text=f"Анти-рейд · Правило 2.6 · Нарушение №{violation_id}")

    mod_role = guild.get_role(server.get("mod_role_id")) if server.get("mod_role_id") else None
    ping_content = f"🚨 {mod_role.mention} — обнаружен возможный рейд ({len(raiders)} входов за {RAID_WINDOW}с)!" if mod_role else None

    raider_ids = [uid for _, uid, _ in raiders]
    view = RaidActionView(guild.id, violation_id, raider_ids)
    try:
        await channel.send(content=ping_content, embed=embed, view=view)
    except discord.HTTPException as e:
        print(f"[RAID-PANEL] ошибка отправки: {e}")


