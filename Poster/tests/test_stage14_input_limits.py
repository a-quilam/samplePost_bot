# tests/test_stage14_input_limits.py
"""
Stage 14, §2: длина ввода — без искусственных лимитов и молчаливых обрезаний.

Реальные ограничения платформенные:
- входящее сообщение Telegram ≤ 4096 символов (лимит API, а не наша
  валидация) — поле не может быть длиннее одного сообщения;
- итоговый текст длиннее 4000 символов разбивается на части
  (split_long_text, Unicode-safe — Stage 10);
- подпись к фото ≤ 1024 (API): при переполнении фото уходит БЗ подписи,
  полный текст — отдельными сообщениями (ничего не обрезается).

Тесты фиксируют отсутствие обрезаний в нашей цепочке:
валидация → сохранение → превью → черновик → рестарт.
"""

import unittest

try:
    from telegram import MessageEntity

    from handlers.post_creation import POST_STEPS
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )
    from utils.validators import validate_url

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as e:  # noqa: BLE001 — причина попадает в сообщение пропуска
    INTEGRATION_IMPORT_ERROR = str(e)

# Пробы «длинного» ввода: обе в пределах одного входящего сообщения
# Telegram (≤ 4096), но длиннее лимита подписи (1000) и длиннее лимита
# разбивки (4000) в сумме со сводкой
LONG_TITLE = ("Длинный заголовок события 🎉 " * 60).strip()
LONG_DESCRIPTION = (
    "Очень длинное описание мероприятия со всеми деталями. " * 68
).strip() + "🏁"


def _norm(text: str) -> str:
    """Нормализация пробелов: разбивка/типографика не должны терять символы."""
    return "".join(text.split())


def _joined_posts(posts: list) -> str:
    return _norm("\n".join(str(p) for p in posts if p))


class NoArbitraryLengthLimitsTests(unittest.TestCase):
    """Валидаторы не накладывают искусственных лимитов длины (§2.1, §2.8)."""

    def test_free_text_fields_have_no_validators(self):
        by_key = {s["key"]: s for s in POST_STEPS}
        for key in ("title", "place_name", "text", "contact"):
            self.assertIsNone(
                by_key[key]["validator"],
                f"поле '{key}' — свободный текст без форматных ограничений",
            )

    def test_url_validator_checks_format_not_length(self):
        # Ссылка с длинным путём — валидна: лимитов длины нет
        long_url = "https://example.com/" + "раздел/подраздел/" * 20 + "конец"
        self.assertTrue(validate_url(long_url), "длинный корректный URL принят")

    def test_probes_fit_real_platform_limits(self):
        # Пробы в пределах реального входящего сообщения Telegram
        self.assertLess(len(LONG_TITLE), 4096)
        self.assertLess(len(LONG_DESCRIPTION), 4096)
        self.assertGreater(len(LONG_TITLE), 1000, "заголовок действительно длинный")
        self.assertGreater(
            len(LONG_DESCRIPTION), 1000, "описание длиннее лимита подписи"
        )


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class LongInputRoundtripTests(unittest.TestCase):
    """§2: длинный форматированный ввод переживает цикл без обрезаний."""

    async def _fill_to_preview(self, h, *, title: str) -> None:
        """Диалог до превью: заголовок (с HTML-жирным), описание, пропуски."""
        await h.text("/start")
        await h.start_dialog()
        # Жирное начало заголовка — HTML-форматирование Stage 10 в цепочке
        await h.text(
            title,
            entities=[MessageEntity(type=MessageEntity.BOLD, offset=0, length=7)],
        )
        for _ in range(4):  # дата / время начала / время конца / место
            await h.text("Пропустить")
        await h.text(LONG_DESCRIPTION)
        await h.text("Пропустить")  # контакт
        await h.text("Пропустить")  # ссылка

    @scenario_test
    async def test_long_text_survives_preview_draft_and_restart(self, h):
        await self._fill_to_preview(h, title=LONG_TITLE)
        await h.text("Пропустить")  # изображение → превью

        # Превью: пост длиннее 4000 разбит на части, содержимое полное
        preview_texts = [p for p in h.posts() if p]
        self.assertGreater(len(preview_texts), 1, "длинный пост разбит на части")
        joined = _joined_posts(preview_texts)
        stripped_tags = joined.replace("<b>", "").replace("</b>", "")
        self.assertIn(
            _norm(LONG_TITLE),
            stripped_tags,
            "заголовок целиком, включая хвост (без молчаливого обрезания)",
        )
        self.assertIn("<b>Длинный</b>", joined, "HTML-форматирование сохранено")
        self.assertIn(
            _norm(LONG_DESCRIPTION),
            joined,
            "описание сохранено целиком (разбивка не теряет символы)",
        )
        self.assertIn("🏁", preview_texts[-1], "хвостовой emoji в последней части")

        # Черновик: сохранение и повторное открытие после рестарта — без потерь
        await h.callback("save_draft")
        await h.restart()
        await h.text("/start")
        await h.callback("drafts")
        await h.callback("editdraft_1")

        reopened = _joined_posts([p for p in h.posts() if p])
        stripped_reopened = reopened.replace("<b>", "").replace("</b>", "")
        self.assertIn(
            _norm(LONG_TITLE), stripped_reopened, "после рестарта заголовок цел"
        )
        self.assertIn(
            _norm(LONG_DESCRIPTION),
            reopened,
            "после рестарта описание целиком",
        )

    @scenario_test
    async def test_long_text_with_photo_sent_fully(self, h):
        # Текст длиннее лимита подписи (1000): фото уходит без подписи,
        # полный текст — отдельными сообщениями (ничего не обрезается)
        await self._fill_to_preview(h, title="Короткий заголовок")
        await h.photo("stage14_fixture_file_id")  # шаг изображения → превью

        posts_before_finish = len(h.post_records())
        await h.callback("finish")

        final = h.post_records()[posts_before_finish:]
        photo_calls = [r for r in final if r["type"] == "photo"]
        self.assertEqual(len(photo_calls), 1, "фото отправлено")
        self.assertIsNone(
            photo_calls[0].get("caption"),
            "текст > 1000 — фото уходит без подписи (лимит подписи API)",
        )

        final_joined = _joined_posts([r.get("text") for r in final if r.get("text")])
        final_stripped = final_joined.replace("<b>", "").replace("</b>", "")
        self.assertIn(_norm("Короткий заголовок"), final_stripped)
        self.assertIn(
            _norm(LONG_DESCRIPTION),
            final_joined,
            "длинный текст дошёл целиком отдельными сообщениями",
        )
        self.assertIn(
            "деталями.🏁",
            final_joined,
            "хвост описания (включая emoji) доехал до конца",
        )


if __name__ == "__main__":
    unittest.main()
