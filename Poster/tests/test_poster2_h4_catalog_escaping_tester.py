# tests/test_poster2_h4_catalog_escaping_tester.py
"""
H4 (Poster 2.3), независимые проверки экранирования каталога текстов.

Проверяется «снаружи»:

1. каждый ключ каталога с переменными экранирует значения (`<`, `>`, `&`,
   `"`, `'` остаются текстом) — независимо от того, `_html` ключ или нет;
2. ключи `_html` сохраняют собственную разметку шаблона (живой `<b>`,
   `<code>`) и при подстановке значения не ломаются;
3. `tr_safe` не экранирует повторно `SafeHtml` (сохранённое форматирование
   значения остаётся живым), а `tr` с тем же шаблоном — экранирует;
4. реальные экраны (карточка места, вопрос конструктора, карточка
   кирпичика) показывают пользовательские данные текстом.

Настоящие `post_bot.db`, `.env` и Telegram не используются.
"""

from __future__ import annotations

import re
import unittest

try:
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        Harness,
        scenario_test,
    )

    INTEGRATION_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_ERROR = str(exc)

# Значение, которое обязано остаться текстом.
EVIL = '<b>x</b>&"y"'

# Разметка, которую каталог вправе использовать в `_html`-ключах.
TRUSTED_TAGS = re.compile(
    r"</?(b|strong|i|em|u|ins|s|strike|del|code|pre|"
    r"blockquote|a|tg-spoiler|span|tg-emoji|tg-time)\b"
)


class CatalogEscapingUnitTests(unittest.TestCase):
    """Прямая проверка контракта tr/tr_safe на всех ключах каталога."""

    def setUp(self) -> None:
        from utils import ui_texts

        self.ui_texts = ui_texts

    def test_every_placeholder_key_escapes_values(self) -> None:
        checked = 0
        with_placeholders = 0
        for key, template in self.ui_texts.DEFAULTS.items():
            names = self.ui_texts._placeholders(template)
            if not names:
                continue
            with_placeholders += 1
            values = {name: EVIL for name in names}
            text = self.ui_texts.tr(key, **values)
            self.assertNotIn("<b>x</b>", text, key)
            self.assertIn("&lt;b&gt;", text, key)
            self.assertIn("&amp;", text, key)
            self.assertIn("&quot;", text, key)
            checked += 1
        self.assertEqual(checked, with_placeholders)
        self.assertGreater(with_placeholders, 250, "ключей с переменными")

    def test_html_keys_keep_own_markup_and_escape_values(self) -> None:
        checked = 0
        for key, template in self.ui_texts.DEFAULTS.items():
            if not key.endswith("_html"):
                continue
            names = self.ui_texts._placeholders(template)
            text = self.ui_texts.tr(key, **{name: EVIL for name in names})
            # Собственная разметка шаблона живая.
            self.assertEqual(
                TRUSTED_TAGS.findall(template),
                TRUSTED_TAGS.findall(text),
                key,
            )
            if names:
                self.assertIn("&lt;b&gt;", text, key)
                self.assertNotIn("<b>x</b>", text, key)
                checked += 1
        self.assertGreater(checked, 20, "ключей _html с переменными")

    def test_plain_keys_do_not_emit_tags_from_template(self) -> None:
        """Даже если редактор вписал тег в обычный ключ — он покажется текстом."""
        from utils import ui_texts

        report = ui_texts.reload_texts(ui_texts.DEFAULT_TEXTS_PATH)
        self.assertTrue(report.ok, report.errors)
        text = ui_texts.tr("menu.cancel.place")
        self.assertNotIn("<", text)

    def test_tr_escapes_but_tr_safe_keeps_sanitized_value(self) -> None:
        from utils.tg_html import sanitize_html

        safe = sanitize_html("<b>жирный</b> & текст")
        safe_text = self.ui_texts.tr_safe("wizard.question.current_value", value=safe)
        self.assertIn("<b>жирный</b>", safe_text)
        self.assertNotIn("&lt;b&gt;", safe_text)

        plain_text = self.ui_texts.tr("wizard.question.current_value", value=safe)
        self.assertNotIn("<b>", plain_text)
        self.assertIn("&lt;b&gt;", plain_text)

    def test_safe_html_does_not_get_escaped_twice(self) -> None:
        """`tr_safe` с SafeHtml: амперсант не превращается в `&amp;amp;`."""
        from utils.tg_html import sanitize_html

        safe = sanitize_html("a & b")
        self.assertEqual(str(safe), "a &amp; b")
        text = self.ui_texts.tr_safe("wizard.question.current_value", value=safe)
        self.assertIn("a &amp; b", text)
        self.assertNotIn("&amp;amp;", text)


@unittest.skipIf(
    INTEGRATION_ERROR, f"интеграционный каркас недоступен: {INTEGRATION_ERROR}"
)
class EscapingScreenTests(unittest.TestCase):
    """Пользовательские значения на реальных экранах остаются текстом."""

    @scenario_test
    async def test_place_creation_steps_escape_typed_name(self, h: Harness) -> None:
        await h.callback("places")
        await h.callback("pl_add")
        await h.text(EVIL)
        text = h.screen_text()
        self.assertNotIn("<b>x</b>", text)
        self.assertIn("&lt;b&gt;", text)
        self.assertIn("&amp;", text)

        await h.callback("pl_create_confirm")
        await h.text("Москва")
        text = h.screen_text()
        self.assertNotIn("<b>x</b>", text)
        self.assertIn("&lt;b&gt;", text)

    @scenario_test
    async def test_admin_place_card_escapes_name_address_and_link(
        self, h: Harness
    ) -> None:
        """Карточка места в кабинете: `admin.*_html` + tr_safe не ломают HTML."""
        from unittest.mock import patch

        from models import Place, PlaceName
        from place_store import normalize_place_name

        session = h.session_factory()
        try:
            place = Place(address=EVIL, url='https://example.com/?a=1&b="2"')
            session.add(place)
            session.flush()
            session.add(
                PlaceName(
                    place_id=place.id,
                    name=EVIL,
                    name_search=normalize_place_name(EVIL),
                    is_canonical=1,
                )
            )
            session.commit()
            place_id = int(place.id)
        finally:
            session.close()

        with patch("config.SUPERADMIN_USER_IDS", (h.user_id,)):
            await h.text("/admin")
            await h.callback(f"adm_place:{place_id}")
        text = h.screen_text()
        self.assertNotIn("<b>x</b>", text)
        self.assertIn("&lt;b&gt;", text)
        self.assertIn("&amp;", text)
        # Ссылка осталась кликабельной, но кавычка в её value экранирована.
        if "<a href=" in text:
            self.assertNotIn('href="https://example.com/?a=1&b="2""', text)

    @scenario_test
    async def test_brick_card_escapes_title_and_description(self, h: Harness) -> None:
        """Карточка кирпичика: название и описание — текст, `_html`-разметка живая."""
        from brick_store import create_personal_brick
        from user_store import get_or_create_user

        session = h.session_factory()
        try:
            get_or_create_user(
                session,
                h.user_id,
                username=f"user{h.user_id}",
                full_name=f"Пользователь {h.user_id}",
            )
            brick = create_personal_brick(
                session,
                actor_telegram_user_id=h.user_id,
                code="evil_brick",
                title=EVIL,
                value_type="short_text",
                description=EVIL,
            )
            brick_id = brick.id
        finally:
            session.close()

        await h.text("/bricks")
        await h.callback(f"bk_open:{brick_id}")
        text = h.screen_text()
        self.assertNotIn("<b>x</b>", text)
        self.assertIn("&lt;b&gt;", text)
        # Разметка самого шаблона карточки осталась живой.
        self.assertIn("<b>", text)


if __name__ == "__main__":
    unittest.main()
