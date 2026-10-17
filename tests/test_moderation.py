"""Offline regression tests for moderation false-positive reductions."""
import asyncio
import json
import unittest
from collections import deque
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import client, llm, moderation, rules, telemetry


class RegexFalsePositiveRegressionTests(unittest.TestCase):
    def setUp(self):
        self._dismissed_before = set(moderation.DISMISSED_PHRASES)
        self._guild_dismissed_before = {
            guild_id: set(phrases)
            for guild_id, phrases in moderation.GUILD_DISMISSED_PHRASES.items()
        }
        self._cache_before = moderation.LLM_CACHE.copy()
        moderation.DISMISSED_PHRASES.clear()
        moderation.GUILD_DISMISSED_PHRASES.clear()
        moderation.LLM_CACHE.clear()
        self.addCleanup(self._restore_moderation_state)

    def _restore_moderation_state(self):
        moderation.DISMISSED_PHRASES.clear()
        moderation.DISMISSED_PHRASES.update(self._dismissed_before)
        moderation.GUILD_DISMISSED_PHRASES.clear()
        moderation.GUILD_DISMISSED_PHRASES.update(self._guild_dismissed_before)
        moderation.LLM_CACHE.clear()
        moderation.LLM_CACHE.update(self._cache_before)

    def test_neutral_passport_think_and_exit_phrases_are_clean(self):
        for text in (
            "ты предложил бесполезный совет",
            "ты задал тупой вопрос",
            "you proposed a stupid idea",
            "Где оформить паспорт?",
            "Я придумаю решение к вечеру.",
            "Выходи из голосового канала, когда закончишь.",
        ):
            with self.subTest(text=text):
                self.assertIsNone(moderation.regex_check(text))

    def test_short_caps_and_emojis_are_not_flood_but_sustained_spam_is(self):
        self.assertIsNone(moderation.regex_check("ОК 😄"))
        self.assertIsNone(moderation.regex_check("ДА!!! 👍"))
        self.assertIsNone(moderation.regex_check("👨‍👩‍👧‍👦" * 5))

        caps = moderation.regex_check("А" * 24)
        self.assertIsNotNone(caps)
        self.assertEqual(caps.rule_id, "3.6")

        emoji_spam = moderation.regex_check("😂" * 10)
        self.assertIsNotNone(emoji_spam)
        self.assertEqual(emoji_spam.rule_id, "3.6")

    def test_ordinary_urls_are_clean_but_invites_and_promotion_are_detected(self):
        for text in (
            "Инструкция: https://steamcommunity.com/help",
            "Сайт проекта: https://example.org/news",
            "Посмотри страницу https://discord.com/features",
            "Не покупайте на https://example.org/shop, это мошенники.",
            "На https://example.org/rules запрещена реклама: «подпишись».",
            "Не ведитесь на free nitro: https://example.org/security",
        ):
            with self.subTest(text=text):
                self.assertIsNone(moderation.regex_check(text))

        invite = moderation.regex_check("Заходи к нам: https://discord.gg/example")
        self.assertIsNotNone(invite)
        self.assertEqual(invite.rule_id, "3.3")

        promotion = moderation.regex_check("Подпишись: https://example.org/channel")
        self.assertIsNotNone(promotion)
        self.assertEqual(promotion.rule_id, "3.3")

        direct_promotion = moderation.regex_check("Покупай https://example.org/shop")
        self.assertIsNotNone(direct_promotion)
        self.assertEqual(direct_promotion.rule_id, "3.3")

    def test_third_party_personal_data_requires_an_asserted_disclosure(self):
        for text in (
            "Его телефон:\n+7 999 123 45 67",
            "Его телефон: `+7 999 123 45 67`",
            "+7 999 123 45 67 — это его телефон",
        ):
            with self.subTest(text=text):
                result = moderation.regex_check(text)
                self.assertIsNotNone(result)
                self.assertEqual(result.rule_id, "3.4")

        for text in (
            "Мой телефон: +7 999 123 45 67",
            'Он написал: «Его телефон: +7 999 123 45 67»',
        ):
            with self.subTest(text=text):
                self.assertIsNone(moderation.regex_check(text))

    def test_directed_threats_and_insults_are_detected(self):
        threat = moderation.regex_check("Я тебя убью")
        self.assertIsNotNone(threat)
        self.assertEqual(threat.rule_id, "3.1")

        for text in (
            "Я тебя деаноню",
            "Я тебя пробью",
            "kill yourself",
            "go die",
            "kys",
            "fick dich",
            "va te faire foutre",
            "you are stupid",
        ):
            with self.subTest(threat=text):
                result = moderation.regex_check(text)
                self.assertIsNotNone(result)
                self.assertEqual(result.rule_id, "3.1")

        insult = moderation.regex_check("Ты тупой")
        self.assertIsNotNone(insult)
        self.assertEqual(insult.rule_id, "3.1")

    def test_banwords_do_not_match_inside_longer_neutral_words_and_still_match_variants(self):
        for text in (
            "Он лохматый и симпатичный.",
            "This is a simple explanation.",
            "Плохой день для прогулки.",
            "просто просто просто",
            "мандарин мандарин мандарин",
        ):
            with self.subTest(text=text):
                self.assertIsNone(moderation.regex_check(text))

        direct = moderation.regex_check("пидор")
        self.assertIsNotNone(direct)
        self.assertTrue(direct.rule_id.startswith("4."))

        obfuscated = moderation.regex_check("п и д о р")
        self.assertIsNotNone(obfuscated)
        self.assertTrue(obfuscated.rule_id.startswith("4."))

        for text in (
            "пидорина", "пидорище", "пидорство", "пидоры", "пидоров",
            "ниггерша", "негров", "дебилоид", "дебилам", "ретарднутый",
            "пид0ра", "d0dikov",
        ):
            with self.subTest(derivative=text):
                result = moderation.regex_check(text)
                self.assertIsNotNone(result)
                self.assertTrue(result.rule_id.startswith("4."))

    def test_all_builtin_banword_entries_remain_detectable(self):
        entries = [
            (rule_id, word)
            for rule_id, words in rules._BANWORD_BASE.items()
            for word in words
        ] + [
            (rule_id, word)
            for rule_id, words in rules._BANWORD_FOREIGN.items()
            for word in words
        ]
        self.assertTrue(entries)

        for rule_id, word in entries:
            text = f"ты {word}" if rule_id == "3.1" else word
            with self.subTest(rule_id=rule_id, word=word):
                self.assertIsNotNone(moderation.regex_check(text))

    def test_split_banword_check_ignores_old_hits_and_catches_a_new_split_word(self):
        old_hit_only = moderation.split_banword_check(
            "пидор привет", "привет"
        )
        self.assertIsNone(old_hit_only)

        split_hit = moderation.split_banword_check("пид ор", "ор")
        self.assertIsNotNone(split_hit)
        self.assertTrue(split_hit.rule_id.startswith("4."))

    def test_dismissal_is_guild_scoped_and_does_not_hide_section_four(self):
        phrase = "ты тупой"
        moderation.mark_phrase_dismissed(phrase, "3.1", guild_id=100)

        self.assertIsNone(moderation.regex_check(phrase, guild_id=100))
        other_guild = moderation.regex_check(phrase, guild_id=200)
        self.assertIsNotNone(other_guild)
        self.assertEqual(other_guild.rule_id, "3.1")

        banned_phrase = "пидор"
        moderation.mark_phrase_dismissed(banned_phrase, "3.1", guild_id=100)
        section_four = moderation.regex_check(banned_phrase, guild_id=100)
        self.assertIsNotNone(section_four)
        self.assertTrue(section_four.rule_id.startswith("4."))

    def test_dismissal_keeps_explicit_server_banwords_and_ignores_empty_entries(self):
        text = "@everyone customword"
        moderation.mark_phrase_dismissed(text, "2.5", guild_id=100)

        custom = moderation.regex_check(
            text, guild_banwords=["customword"], guild_id=100
        )
        self.assertIsNotNone(custom)
        self.assertEqual(custom.rule_id, "server")

        for words in (["", "   "], ["!!!", "_"]):
            with self.subTest(words=words):
                self.assertIsNone(moderation.regex_check("Обычное сообщение", words))

    def test_mass_ping_requires_the_complete_ping_token(self):
        self.assertIsNone(moderation.regex_check("@hereafter"))
        self.assertEqual(moderation.regex_check("@here").rule_id, "2.5")


class MessageContextRegressionTests(unittest.TestCase):
    def test_build_message_context_excludes_current_and_future_entries(self):
        now = 1_800_000_000.0
        key = (501, 601)
        entries = deque([
            {"id": 99, "channel": 701, "time": now - 5, "raw": "предыдущее"},
            {"id": 100, "channel": 701, "time": now, "raw": "текущий дубль"},
            {"id": 101, "channel": 701, "time": now + 1, "raw": "будущее"},
            {"id": 98, "channel": 702, "time": now - 2, "raw": "другой канал"},
            {"id": 97, "channel": 701, "time": now - 70, "raw": "слишком старое"},
        ])
        message = SimpleNamespace(
            guild=SimpleNamespace(id=key[0]),
            author=SimpleNamespace(id=key[1]),
            channel=SimpleNamespace(id=701),
            id=100,
            content="сейчас",
            created_at=datetime.fromtimestamp(now, tz=timezone.utc),
            reference=None,
        )

        with patch.dict(client.RECENT_MESSAGES, {key: entries}, clear=False):
            combined, context = client.build_message_context(message)

        self.assertEqual(combined, "предыдущее сейчас")
        self.assertIn("предыдущее", context)
        self.assertNotIn("текущий дубль", context)
        self.assertNotIn("будущее", context)
        self.assertNotIn("другой канал", context)
        self.assertNotIn("слишком старое", context)

    def test_dismissed_examples_do_not_cross_guild_boundaries(self):
        fake_db = SimpleNamespace(
            list_dismissed_examples=lambda guild_id, limit: []
        )
        guild_cache = {
            10: deque(["dismissed on guild ten"]),
            20: deque(["dismissed on guild twenty"]),
        }
        with (
            patch.object(telemetry, "_DISMISSED_CACHE", guild_cache),
            patch.object(telemetry, "_ensure_loaded_from_disk"),
            patch("bot.database.get_db", return_value=fake_db),
        ):
            self.assertEqual(
                telemetry.load_dismissed_examples(guild_id=10),
                ["dismissed on guild ten"],
            )
            self.assertEqual(
                telemetry.load_dismissed_examples(guild_id=20),
                ["dismissed on guild twenty"],
            )


class FloodExceptionRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def _run_message_with_exceptions(self, exceptions):
        database = SimpleNamespace(
            record_discord_member=lambda *_args: None,
            list_rule_exceptions=lambda *_args: exceptions,
        )
        message = SimpleNamespace(
            author=SimpleNamespace(bot=False, id=22),
            guild=SimpleNamespace(id=11),
            channel=SimpleNamespace(id=33, name="general"),
            id=44,
            content="repeat",
            delete=AsyncMock(),
        )
        bot = SimpleNamespace(
            _bg_tasks=[],
            _handle_message_moderation=AsyncMock(),
        )
        with (
            patch.object(client, "get_db", return_value=database),
            patch.object(client, "record_message"),
            patch.object(client, "channel_restriction_delete", new=AsyncMock(return_value=False)),
            patch.object(client, "flood_count", return_value=3),
            redirect_stdout(StringIO()),
        ):
            await client.ModBot.on_message(bot, message)
        return message, bot

    async def test_channel_rule_exception_prevents_flood_delete(self):
        message, bot = await self._run_message_with_exceptions(["3.6"])

        message.delete.assert_not_awaited()
        bot._handle_message_moderation.assert_awaited_once_with(message, None)

    async def test_flood_delete_still_happens_without_channel_exception(self):
        message, bot = await self._run_message_with_exceptions([])

        message.delete.assert_awaited_once()
        bot._handle_message_moderation.assert_not_awaited()


class LLMValidationAndCacheRegressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._cache_before = moderation.LLM_CACHE.copy()
        moderation.LLM_CACHE.clear()
        self.addCleanup(self._restore_cache)

    def _restore_cache(self):
        moderation.LLM_CACHE.clear()
        moderation.LLM_CACHE.update(self._cache_before)

    def test_llm_verdict_requires_valid_rule_severity_confidence_and_current_evidence(self):
        valid = {
            "violation": True,
            "rule_id": "3.1",
            "severity": "high",
            "reason": "Личное оскорбление",
            "translation": None,
            "confidence": 0.95,
            "evidence": "Ты тупой",
        }
        source = "Ты тупой"
        invalid_cases = (
            ("unknown rule", {**valid, "rule_id": "9.9"}, source),
            ("unknown severity", {**valid, "severity": "critical"}, source),
            ("low confidence", {**valid, "confidence": 0.84}, source),
            ("boolean confidence", {**valid, "confidence": True}, source),
            ("empty evidence", {**valid, "evidence": " "}, source),
            ("context-only evidence", {**valid, "evidence": "ты тупой"}, "привет"),
        )

        with patch("bot.telemetry.log_event"):
            for label, verdict, current_message in invalid_cases:
                with self.subTest(case=label):
                    parsed = moderation._parse_llm_json(
                        json.dumps(verdict, ensure_ascii=False), source=current_message
                    )
                    self.assertIsNone(parsed)

            parsed_valid = moderation._parse_llm_json(
                json.dumps(valid, ensure_ascii=False), source=source
            )

        self.assertEqual(parsed_valid, valid)

    async def test_llm_cache_isolated_by_guild_and_custom_banwords(self):
        clean_result = json.dumps({
            "violation": False,
            "rule_id": None,
            "severity": None,
            "reason": "",
            "translation": None,
        })
        complete = AsyncMock(return_value=clean_result)
        with (
            patch.object(llm, "available_provider", return_value=object()),
            patch.object(llm, "complete", new=complete),
            patch.object(moderation, "_collect_dismissed_examples", return_value=[]),
            patch("bot.telemetry.log_event"),
        ):
            await moderation.llm_check_with_translation(
                "Обычная фраза для кэша", [], guild_id=10
            )
            await moderation.llm_check_with_translation(
                "Обычная фраза для кэша", [], guild_id=10
            )
            await moderation.llm_check_with_translation(
                "Обычная фраза для кэша", [], guild_id=20
            )
            await moderation.llm_check_with_translation(
                "Обычная фраза для кэша", ["custom-banword"], guild_id=10
            )

        self.assertEqual(complete.await_count, 3)


if __name__ == "__main__":
    unittest.main()
