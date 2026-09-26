"""Discord UI Views for the moderation panel (one message, all components).

Кнопки персистентны: custom_id кодирует id нарушения, поэтому после перезапуска
бота они перерегистрируются через DynamicItem / bot.add_view и продолжают работать.
После нажатия панель редактируется: помечается вынесенное модератором наказание.
"""
import asyncio
import re
import time as _time
from datetime import timedelta

import discord
from discord import ui

BRAND_GREEN = 0x16BE78
MAX_TIMEOUT_SECONDS = 28 * 86400  # Лимит Discord API на тайм-аут — 28 суток

_PERM_REQUIREMENTS = {
    "ban": "Ban Members / Роль модерации / Administrator",
    "kick": "Kick Members / Роль модерации / Administrator",
    "timeout": "Moderate Members / Manage Messages / Роль модерации",
    "delete": "Manage Messages / Moderate Members / Роль модерации",
    "skip": "Manage Messages / Moderate Members / Роль модерации",
    "dismiss": "Manage Messages / Moderate Members / Роль модерации",
}

_PERMANENT_TOKENS = frozenset({
    "0", "перманентно", "перманентный", "пермач", "навсегда",
    "perm", "permanent", "forever", "inf", "∞", "-",
})


def is_permanent_duration(value: str) -> bool:
    """True, если строка обозначает перманентный (бессрочный) бан."""
    return (value or "").strip().lower() in _PERMANENT_TOKENS


class TimeoutModal(ui.Modal):
    def __init__(self, callback, modal_title: str = "Длительность",
                 default_value: str = "", label: str = None):
        super().__init__(title=modal_title)
        self._cb = callback
        self.duration = ui.TextInput(
            label=label or "Длительность (минуты/часы/дни, напр. 60, 3д)",
            placeholder=default_value or "60",
            default=default_value or None,
            required=True,
            max_length=20,
        )
        self.add_item(self.duration)

    async def on_submit(self, interaction: discord.Interaction):
        await self._cb(interaction, self.duration.value)


class NickModal(ui.Modal):
    def __init__(self, callback, default_nick: str = ""):
        super().__init__(title="Смена никнейма участника")
        self._cb = callback
        self.new_nick = ui.TextInput(
            label="Новый никнейм (пусто — сбросить серверный)",
            placeholder=default_nick or "Участник",
            default=default_nick or None,
            required=False,
            max_length=32,
        )
        self.add_item(self.new_nick)

    async def on_submit(self, interaction: discord.Interaction):
        await self._cb(interaction, (self.new_nick.value or "").strip())


class ModActionView(ui.View):
    """Персистентные кнопки решения по нарушению (#violation_id в custom_id).

    «suggest» — рекомендация по правилу, посчитанная по числу нарушений:
    {"timeout": сек|None, "ban": сек|None, "action": str}. Длительности
    показываются прямо на кнопках и подставляются в модалки по умолчанию.
    """

    def __init__(self, guild_id: int, user_id: int, violation_id: int, target_msg_id: int = None,
                 channel_id: int = None, lang: str = "ru", suggest: dict = None,
                 show_nick_btn: bool = False, reporter_id: int = None):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.user_id = user_id
        self.violation_id = violation_id
        self.target_msg_id = target_msg_id
        self.channel_id = channel_id
        self.lang = lang
        self.suggest = suggest or {}
        self.show_nick_btn = show_nick_btn
        self.reporter_id = reporter_id
        self._build_buttons()

    @staticmethod
    def _cid(action: str, violation_id: int) -> str:
        return f"pv:{violation_id}:{action}"

    def _make(self, action: str, label: str, style, callback, row: int = 0) -> None:
        btn = ui.Button(label=label, style=style, custom_id=self._cid(action, self.violation_id),
                        row=row)
        btn.callback = callback
        self.add_item(btn)

    def _suggest_label(self, kind: str, fallback: str) -> str:
        from bot.rules import fmt_duration
        secs = self.suggest.get(kind)
        return f"{fallback} {fmt_duration(secs)}" if secs else fallback

    def _build_buttons(self) -> None:
        self._make("dismiss", "✖️ Не нарушение", discord.ButtonStyle.secondary,
                   self.dismiss_cb, row=0)
        self._make("delete", "🗑 Удалить", discord.ButtonStyle.danger, self.delete_cb, row=0)
        self._make("timeout", self._suggest_label("timeout", "⏳ Мут"),
                   discord.ButtonStyle.primary, self.timeout_cb, row=0)
        self._make("kick", "👢 Кик", discord.ButtonStyle.secondary, self.kick_cb, row=0)
        self._make("ban", self._suggest_label("ban", "🔨 Бан"),
                   discord.ButtonStyle.danger, self.ban_cb, row=0)
        self._make("skip", "➡️ Пропустить", discord.ButtonStyle.success, self.skip_cb, row=1)
        if self.show_nick_btn:
            self._make("nick", "✏️ Сменить ник", discord.ButtonStyle.primary, self.nick_cb, row=1)
        if self.reporter_id:
            self._make("falsereport", "⚠️ Ложный вызов (п. 2.5)", discord.ButtonStyle.danger,
                       self.falsereport_cb, row=1)


    def _perm(self, interaction: discord.Interaction, action: str = "delete") -> bool:
        perms = getattr(interaction.user, "guild_permissions", None)
        if perms is None:
            return False
        if perms.administrator:
            return True
        # Проверка настроенной роли модерации сервера
        try:
            from bot.database import get_db
            server = get_db().get_server(self.guild_id)
            mod_role_id = server.get("mod_role_id")
            if mod_role_id and any(r.id == mod_role_id for r in getattr(interaction.user, "roles", ())):
                return True
        except Exception:
            pass

        if action == "ban":
            return perms.ban_members
        if action == "kick":
            return perms.kick_members
        if action == "timeout":
            return perms.moderate_members or perms.manage_messages
        return perms.manage_messages or perms.moderate_members

    async def _denied(self, interaction: discord.Interaction, action: str = "delete"):
        req = _PERM_REQUIREMENTS.get(action, "Manage Messages / Administrator")
        await interaction.response.send_message(
            f"Недостаточно прав (требуется: {req}).", ephemeral=True
        )

    async def _resolve_member(self, guild: discord.Guild) -> discord.Member | None:
        member = guild.get_member(self.user_id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(self.user_id)
        except (discord.NotFound, discord.HTTPException):
            return None

    async def _original(self, interaction: discord.Interaction):
        """Оригинальное сообщение из того канала, где его написали."""
        if not self.target_msg_id or not self.channel_id:
            return None, None
        ch = interaction.client.get_channel(self.channel_id)
        if ch is None:
            return None, None
        try:
            return ch, await ch.fetch_message(self.target_msg_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return ch, None

    async def _mark_punished(self, interaction: discord.Interaction, action: str, detail: str = ""):
        """Дописывает в панель, что модератор вынес наказание, и блокирует кнопки."""
        try:
            if not interaction.message or not interaction.message.embeds:
                return
            embed = discord.Embed.from_dict((interaction.message.embeds[0]).to_dict())
            stamp = _time.strftime("%d.%m %H:%M")
            embed.add_field(
                name="⚖ Наказание вынесено",
                value=f"**{action}** {detail}\nМодератор: {interaction.user.mention} · {stamp}",
                inline=False,
            )
            embed.color = discord.Color(BRAND_GREEN)
            for child in self.children:
                if isinstance(child, ui.Button):
                    child.disabled = True
            await interaction.message.edit(embed=embed, view=self)
        except (discord.HTTPException, discord.NotFound):
            pass

    # ---- callbacks (interaction-only signature) ----
    async def delete_cb(self, interaction: discord.Interaction):
        if not self._perm(interaction, "delete"):
            await self._denied(interaction, "delete")
            return
        _, target = await self._original(interaction)
        if target is None:
            await interaction.response.send_message(
                "Сообщение не найдено (удалено или обработано Akemi).",
                ephemeral=True,
            )
        else:
            try:
                await target.delete()
                await interaction.response.send_message(
                    "Сообщение удалено.", ephemeral=True
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                await interaction.response.send_message(
                    "Нет прав удалить это сообщение.", ephemeral=True
                )
        await self._mark_punished(interaction, "Удаление")
        await self._log_punishment(interaction, "delete")

    async def timeout_cb(self, interaction: discord.Interaction):
        if not self._perm(interaction, "timeout"):
            await self._denied(interaction, "timeout")
            return
        from bot.rules import fmt_duration
        default = fmt_duration(self.suggest.get("timeout")) if self.suggest.get("timeout") else ""
        modal = TimeoutModal(
            self._handle_timeout,
            "Тайм-аут (длительность)",
            default_value=default,
            label="Длительность (напр. 60м, 3ч, 7д; макс. 28д)",
        )
        await interaction.response.send_modal(modal)

    async def kick_cb(self, interaction: discord.Interaction):
        if not self._perm(interaction, "kick"):
            await self._denied(interaction, "kick")
            return
        member = await self._resolve_member(interaction.guild)
        if member:
            try:
                await member.kick(reason=f"Violation #{self.violation_id}")
                await interaction.response.send_message(
                    f"Кик применён к {member.mention}.",
                    ephemeral=True,
                )
                await self._mark_punished(interaction, "Кик")
                await self._log_punishment(interaction, "kick")
            except discord.Forbidden:
                await interaction.response.send_message("Недостаточно прав для кика.", ephemeral=True)
            except discord.HTTPException as e:
                await interaction.response.send_message(f"Ошибка Discord API при кике: {e}", ephemeral=True)
        else:
            await interaction.response.send_message("Пользователь вне сервера.", ephemeral=True)

    async def ban_cb(self, interaction: discord.Interaction):
        if not self._perm(interaction, "ban"):
            await self._denied(interaction, "ban")
            return
        from bot.rules import fmt_duration
        ban_secs = self.suggest.get("ban")
        default = fmt_duration(ban_secs) if ban_secs else "перманентно"
        modal = TimeoutModal(
            self._handle_ban,
            "Бан (длительность в днях)",
            default_value=default,
            label="Длительность (напр. 7д, 14д или перманентно)",
        )
        await interaction.response.send_modal(modal)

    async def skip_cb(self, interaction: discord.Interaction):
        if not self._perm(interaction, "skip"):
            await self._denied(interaction, "skip")
            return
        await interaction.response.send_message("Пропущено — без наказания.", ephemeral=True)
        await self._mark_punished(interaction, "Пропущено (без наказания)")
        await self._log_punishment(interaction, "skip")

    async def dismiss_cb(self, interaction: discord.Interaction):
        """«Не нарушение» — панель ложного срабатывания: не наказываем, не удаляем
        сообщение, страйк снимается (исключается из счётчика нарушений)."""
        if not self._perm(interaction, "dismiss"):
            await self._denied(interaction, "dismiss")
            return
        await interaction.response.send_message(
            "Панель снята — не считается нарушением.", ephemeral=True)
        await self._mark_punished(interaction, "Не нарушение (панель снята)")
        await self._log_punishment(interaction, "dismiss", status="dismissed")

    async def nick_cb(self, interaction: discord.Interaction):
        """Кнопка «✏️ Сменить ник» в панелях нарушений профиля (п. 2.2, 2.3, 3.7)."""
        if not self._perm(interaction, "timeout"):
            await self._denied(interaction, "timeout")
            return
        default_nick = f"Участник #{str(self.user_id)[-4:]}"
        modal = NickModal(self._handle_nick, default_nick=default_nick)
        await interaction.response.send_modal(modal)

    async def _handle_nick(self, interaction: discord.Interaction, new_nick: str):
        member = await self._resolve_member(interaction.guild)
        if not member:
            await interaction.response.send_message("Пользователь вне сервера.", ephemeral=True)
            return
        target_nick = new_nick if new_nick else None
        try:
            await member.edit(nick=target_nick, reason=f"Violation #{self.violation_id} (п. 3.7 / 2.2)")
            label_nick = f"«{target_nick}»" if target_nick else "сброшен (по умолчанию)"
            await interaction.response.send_message(
                f"Никнейм {member.mention} изменён: **{label_nick}**.", ephemeral=True
            )
            await self._mark_punished(interaction, "Смена никнейма", label_nick)
        except discord.Forbidden:
            await interaction.response.send_message(
                "Недостаточно прав у бота для смены никнейма (роль бота должна быть выше роли участника).",
                ephemeral=True,
            )
        except discord.HTTPException as e:
            await interaction.response.send_message(f"Ошибка Discord API: {e}", ephemeral=True)

    async def falsereport_cb(self, interaction: discord.Interaction):
        """Кнопка «⚠️ Ложный вызов (п. 2.5)» — наказывает автора ложной жалобы тайм-аутом на 60 минут."""
        if not self._perm(interaction, "timeout"):
            await self._denied(interaction, "timeout")
            return
        reporter_uid = self.reporter_id
        if not reporter_uid:
            # Пробуем извлечь ID жалобщика из текста нарушения, если кнопка нажата после рестарта
            from bot.database import get_db
            v = get_db().get_violation(self.violation_id)
            if v and v.get("original_text"):
                m = re.search(r"reporter_id=(\d+)", v["original_text"])
                if m:
                    reporter_uid = int(m.group(1))
        if not reporter_uid:
            await interaction.response.send_message("Не удалось определить автора жалобы.", ephemeral=True)
            return
        reporter = interaction.guild.get_member(reporter_uid)
        if reporter is None:
            try:
                reporter = await interaction.guild.fetch_member(reporter_uid)
            except (discord.NotFound, discord.HTTPException):
                reporter = None
        if not reporter:
            await interaction.response.send_message("Автор жалобы уже покинул сервер.", ephemeral=True)
            return
        try:
            delta = timedelta(minutes=60)
            await reporter.timeout(delta, reason=f"Правило 2.5: Ложный вызов модерации (Report #{self.violation_id})")
            from bot.database import get_db
            db = get_db()
            rep_vid = db.add_violation(
                self.guild_id, reporter.id,
                rule_id="2.5", severity="medium", method="manual_report",
                message_snapshot="Ложная жалоба / злоупотребление /report",
                original_text="Ложная жалоба / злоупотребление /report",
                channel_id=self.channel_id or 0, message_id=0,
            )
            db.add_punishment(
                self.guild_id, reporter.id, violation_id=rep_vid,
                moderator_id=interaction.user.id, action="timeout", duration_seconds=3600,
            )
            await interaction.response.send_message(
                f"Жалоба отклонена как ложная. Автору жалобы {reporter.mention} выдан тайм-аут **60м** по **п. 2.5**.",
                ephemeral=True,
            )
            await self._mark_punished(
                interaction, "Ложный вызов (п. 2.5)", f"Тайм-аут 60м автору жалобы {reporter.mention}"
            )
            await self._log_punishment(interaction, "dismiss", status="dismissed")
        except discord.Forbidden:
            await interaction.response.send_message(
                "Недостаточно прав для выдачи тайм-аута автору жалобы.", ephemeral=True
            )
        except discord.HTTPException as e:
            await interaction.response.send_message(f"Ошибка Discord API: {e}", ephemeral=True)

    # ---- helpers ----
    async def _handle_timeout(self, interaction: discord.Interaction, duration_str: str):
        seconds = parse_duration(duration_str, default_unit="m")
        if seconds is None or seconds <= 0:
            await interaction.response.send_message("Неверный формат длительности.", ephemeral=True)
            return
        clamped = False
        if seconds > MAX_TIMEOUT_SECONDS:
            seconds = MAX_TIMEOUT_SECONDS
            clamped = True
        member = await self._resolve_member(interaction.guild)
        if not member:
            await interaction.response.send_message("Пользователь вне сервера.", ephemeral=True)
            return
        try:
            delta = timedelta(seconds=seconds)
            await member.timeout(delta, reason=f"Violation #{self.violation_id}")
            clamp_note = " (ограничено лимитом Discord в 28 дней)" if clamped else ""
            await interaction.response.send_message(
                f"Тайм-аут **{delta}**{clamp_note} применён к {member.mention}.",
                ephemeral=True,
            )
            await self._mark_punished(interaction, "Тайм-аут", str(delta))
            await self._log_punishment(interaction, "timeout", seconds)
        except discord.Forbidden:
            await interaction.response.send_message("Недостаточно прав для тайм-аута.", ephemeral=True)
        except discord.HTTPException as e:
            await interaction.response.send_message(f"Ошибка Discord API при тайм-ауте: {e}", ephemeral=True)

    async def _handle_ban(self, interaction: discord.Interaction, duration_str: str):
        permanent = is_permanent_duration(duration_str)
        if permanent:
            days = None
            duration_seconds = 0
        else:
            parsed = parse_duration(duration_str, default_unit="d")
            if parsed <= 0:
                await interaction.response.send_message(
                    "Неверный формат длительности (укажите дни, напр. `7д`, или `перманентно`).",
                    ephemeral=True,
                )
                return
            days = max(round(parsed / 86400), 1)
            duration_seconds = days * 86400

        member = await self._resolve_member(interaction.guild)
        target = member or discord.Object(id=self.user_id)
        target_mention = member.mention if member else f"<@{self.user_id}>"
        del_days = min(days, 7) if days is not None else 7

        try:
            await interaction.guild.ban(
                target,
                reason=f"Violation #{self.violation_id}",
                delete_message_days=del_days,
            )
            if days is None:
                await interaction.response.send_message(
                    f"Перманентный бан применён к {target_mention}.",
                    ephemeral=True,
                )
                await self._mark_punished(interaction, "Бан", "перманентно")
                await self._log_punishment(interaction, "ban", 0)
            else:
                await interaction.response.send_message(
                    f"Бан **{days} дн.** применён к {target_mention}. Авторазбан "
                    f"через {days} дн.",
                    ephemeral=True,
                )
                await self._mark_punished(interaction, "Бан", f"{days} дн.")
                punishment_id = await self._log_punishment(interaction, "ban", duration_seconds)
                asyncio.create_task(self._auto_unban(interaction.guild, days, punishment_id))
        except discord.Forbidden:
            await interaction.response.send_message("Недостаточно прав для бана.", ephemeral=True)
        except discord.HTTPException as e:
            await interaction.response.send_message(f"Ошибка Discord API при бане: {e}", ephemeral=True)

    async def _auto_unban(self, guild: discord.Guild, days: int, punishment_id: int = None):
        """Фоновый таймер авторазбана (дублируется проверкой по БД в ModBot._unban_loop)."""
        try:
            await asyncio.sleep(days * 86400)
            await guild.unban(
                discord.Object(id=self.user_id),
                reason=f"Авторазбан по истечении {days} дн. (Violation #{self.violation_id})",
            )
            if punishment_id is not None:
                from bot.database import get_db
                get_db().mark_punishment_status(punishment_id, "unbanned")
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            if punishment_id is not None:
                try:
                    from bot.database import get_db
                    get_db().mark_punishment_status(punishment_id, "unbanned")
                except Exception:
                    pass
        except asyncio.CancelledError:
            pass

    async def _log_punishment(self, interaction: discord.Interaction, action: str,
                              duration_seconds: int = 0, status: str = "applied") -> int | None:
        from bot.database import get_db
        from bot.telemetry import log_event
        db = get_db()
        punishment_id = None
        try:
            punishment_id = db.add_punishment(
                self.guild_id, self.user_id,
                violation_id=self.violation_id,
                moderator_id=interaction.user.id,
                action=action,
                duration_seconds=duration_seconds,
                status=status,
            )
        except Exception as e:
            print(f"[PUNISH] не удалось записать наказание: {e}")
        # Телеметрия: решение модератора по конкретному нарушению — сигнал для обучения.
        try:
            v = db.get_violation(self.violation_id)
        except Exception:
            v = None
        if action == "dismiss" and v:
            try:
                from bot.moderation import mark_phrase_dismissed
                mark_phrase_dismissed(v.get("original_text") or "", v.get("rule_id"))
            except Exception as e:
                print(f"[DISMISS] mark_phrase_dismissed: {e}")
        try:
            log_event(
                "moderator_action",
                guild_id=self.guild_id,
                user_id=self.user_id,
                violation_id=self.violation_id,
                moderator_id=interaction.user.id,
                action=action,
                duration_seconds=duration_seconds,
                status=status,
                rule_id=v.get("rule_id") if v else None,
                severity=v.get("severity") if v else None,
                method=v.get("method") if v else None,
                detected_language=v.get("detected_language") if v else None,
                text=v.get("original_text") if v else None,
                translated_text=v.get("translated_text") if v else None,
                time_to_decision=v.get("created_at") if v else None,
            )
        except Exception as e:
            print(f"[TELEMETRY] moderator_action: {e}")
        return punishment_id


class RaidActionView(ui.View):
    """Боевые кнопки для панели Анти-Рейда (Пункт 2.6 «Набеги и рейды»):
      1) 🔨 Забанить всю волну рейда (в 1 клик банит всех участников из списка всплеска);
      2) 🔒 Пауза инвайтов / Локдаун (30 мин);
      3) ✖️ Ложная тревога.
    """

    def __init__(self, guild_id: int, violation_id: int, raider_ids: list[int]):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.violation_id = violation_id
        self.raider_ids = list(dict.fromkeys(raider_ids))

        btn_ban = ui.Button(
            label=f"🔨 Забанить всю волну ({len(self.raider_ids)})",
            style=discord.ButtonStyle.danger,
            custom_id=f"raid:{violation_id}:banwave",
            row=0,
        )
        btn_ban.callback = self.ban_wave_cb
        self.add_item(btn_ban)

        btn_lock = ui.Button(
            label="🔒 Пауза инвайтов (30 мин)",
            style=discord.ButtonStyle.primary,
            custom_id=f"raid:{violation_id}:lockdown",
            row=0,
        )
        btn_lock.callback = self.lockdown_cb
        self.add_item(btn_lock)

        btn_dismiss = ui.Button(
            label="✖️ Ложная тревога",
            style=discord.ButtonStyle.secondary,
            custom_id=f"raid:{violation_id}:dismiss",
            row=0,
        )
        btn_dismiss.callback = self.dismiss_raid_cb
        self.add_item(btn_dismiss)

    def _has_perm(self, interaction: discord.Interaction, action: str = "ban") -> bool:
        v = ModActionView(self.guild_id, 0, self.violation_id)
        return v._perm(interaction, action)

    async def ban_wave_cb(self, interaction: discord.Interaction):
        if not self._has_perm(interaction, "ban"):
            await interaction.response.send_message(
                "Недостаточно прав (требуется Ban Members / Роль модерации / Administrator).",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        from bot.database import get_db
        db = get_db()
        banned = 0
        failed = 0
        for uid in self.raider_ids:
            try:
                await interaction.guild.ban(
                    discord.Object(id=uid),
                    reason=f"Анти-рейд п. 2.6 (Волна #{self.violation_id})",
                    delete_message_days=1,
                )
                db.add_punishment(
                    self.guild_id, uid,
                    violation_id=self.violation_id,
                    moderator_id=interaction.user.id,
                    action="ban",
                    duration_seconds=0,
                    status="applied",
                )
                banned += 1
            except discord.HTTPException:
                failed += 1
        try:
            if interaction.message and interaction.message.embeds:
                embed = discord.Embed.from_dict(interaction.message.embeds[0].to_dict())
                stamp = _time.strftime("%d.%m %H:%M")
                embed.add_field(
                    name="⚖ Волна рейда заблокирована",
                    value=(
                        f"Забанено: **{banned}** из {len(self.raider_ids)} "
                        f"{f'(ошибок: {failed})' if failed else ''}\n"
                        f"Модератор: {interaction.user.mention} · {stamp}"
                    ),
                    inline=False,
                )
                embed.color = discord.Color(BRAND_GREEN)
                for child in self.children:
                    if isinstance(child, ui.Button) and "banwave" in (child.custom_id or ""):
                        child.disabled = True
                await interaction.message.edit(embed=embed, view=self)
        except discord.HTTPException:
            pass
        await interaction.followup.send(
            f"🔨 Волна рейда обработана: забанено **{banned}** аккаунтов (п. 2.6).",
            ephemeral=True,
        )

    async def lockdown_cb(self, interaction: discord.Interaction):
        if not self._has_perm(interaction, "timeout"):
            await interaction.response.send_message("Недостаточно прав для включения защиты.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        paused_invites = False
        slowmode_channels = 0
        # 1. Пытаемся поставить на паузу приглашения Discord (invites_disabled)
        try:
            await guild.edit(invites_disabled=True, reason=f"Анти-рейд локдаун (30 мин) от {interaction.user}")
            paused_invites = True
        except (discord.Forbidden, discord.HTTPException, TypeError):
            paused_invites = False

        # 2. Дополнительно включаем медленный режим (10 сек) в текстовых каналах без slowmode
        changed_channels = []
        for ch in guild.text_channels:
            if ch.slowmode_delay == 0 and ch.permissions_for(guild.me).manage_channels:
                try:
                    await ch.edit(slowmode_delay=10, reason="Анти-рейд защита (30 мин)")
                    changed_channels.append(ch.id)
                    slowmode_channels += 1
                except discord.HTTPException:
                    pass

        details = []
        if paused_invites:
            details.append("инвайты поставлены на паузу")
        if slowmode_channels:
            details.append(f"медленный режим (10с) включён в {slowmode_channels} каналах")
        if not details:
            await interaction.followup.send(
                "Не удалось включить паузу инвайтов (проверьте права Manage Guild / Manage Channels у бота).",
                ephemeral=True,
            )
            return

        summary = ", ".join(details) + " на 30 минут"
        await interaction.followup.send(f"🔒 Защита включена: {summary}.", ephemeral=True)

        async def _lift_lockdown():
            await asyncio.sleep(1800)
            if paused_invites:
                try:
                    await guild.edit(invites_disabled=False, reason="Авто-снятие анти-рейд паузы (30 мин)")
                except Exception:
                    pass
            for cid in changed_channels:
                ch_obj = guild.get_channel(cid)
                if ch_obj and ch_obj.slowmode_delay == 10:
                    try:
                        await ch_obj.edit(slowmode_delay=0, reason="Авто-снятие анти-рейд slowmode")
                    except Exception:
                        pass

        asyncio.create_task(_lift_lockdown())

    async def dismiss_raid_cb(self, interaction: discord.Interaction):
        if not self._has_perm(interaction, "dismiss"):
            await interaction.response.send_message("Недостаточно прав.", ephemeral=True)
            return
        from bot.database import get_db
        get_db().add_punishment(
            self.guild_id, 0,
            violation_id=self.violation_id,
            moderator_id=interaction.user.id,
            action="dismiss",
            status="dismissed",
        )
        try:
            if interaction.message and interaction.message.embeds:
                embed = discord.Embed.from_dict(interaction.message.embeds[0].to_dict())
                embed.color = discord.Color(BRAND_GREEN)
                embed.add_field(
                    name="⚖ Статус",
                    value=f"**Ложная тревога (снято)** — {interaction.user.mention}",
                    inline=False,
                )
                for child in self.children:
                    if isinstance(child, ui.Button):
                        child.disabled = True
                await interaction.message.edit(embed=embed, view=self)
        except discord.HTTPException:
            pass
        await interaction.response.send_message("Панель анти-рейда снята.", ephemeral=True)


class DynamicModButton(
    ui.DynamicItem[ui.Button],
    template=r"pv:(?P<vid>\d+):(?P<action>[a-z]+)",
):
    """Динамический обработчик персистентных кнопок для любых панелей из БД (без лимита в 200 штук)."""

    def __init__(self, violation_id: int, action: str):
        super().__init__(
            ui.Button(
                style=discord.ButtonStyle.secondary,
                custom_id=f"pv:{violation_id}:{action}",
            )
        )
        self.violation_id = violation_id
        self.action = action

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: ui.Button,
        match: re.Match[str],
    ):
        return cls(int(match.group("vid")), match.group("action"))

    async def callback(self, interaction: discord.Interaction) -> None:
        from bot.database import get_db
        from bot.rules import build_suggest
        db = get_db()
        v = db.get_violation(self.violation_id)
        if not v:
            await interaction.response.send_message(
                f"Нарушение #{self.violation_id} не найдено в базе данных.",
                ephemeral=True,
            )
            return
        n = db.count_real_violations(v["guild_id"], v["user_id"])
        suggest = build_suggest(v.get("rule_id"), n, v.get("severity") or "medium")
        view = ModActionView(
            v["guild_id"],
            v["user_id"],
            v["id"],
            target_msg_id=v.get("message_id"),
            channel_id=v.get("channel_id"),
            suggest=suggest,
            show_nick_btn=(self.action == "nick"),
        )
        handler = getattr(view, f"{self.action}_cb", None)
        if handler is None:
            await interaction.response.send_message("Неизвестное действие.", ephemeral=True)
            return
        await handler(interaction)



def parse_duration(value: str, default_unit: str = "m") -> int:
    """Parse a duration string into seconds. Supports: '60', '2ч', '1d', '7д', '1h'."""
    value = (value or "").strip().lower()
    if not value or is_permanent_duration(value):
        return 0
    try:
        multipliers = {
            "с": 1, "s": 1,
            "м": 60, "m": 60,
            "ч": 3600, "h": 3600,
            "д": 86400, "d": 86400,
            "н": 604800, "w": 604800,
        }
        num_str = ""
        unit = None
        for ch in value:
            if ch.isdigit():
                num_str += ch
            elif not ch.isspace():
                unit = ch
                break
        if not num_str:
            return 0
        number = int(num_str)
        default_mult = multipliers.get(default_unit, 60)
        if unit is None:
            return number * default_mult
        return number * multipliers.get(unit, default_mult)
    except Exception:
        return 0