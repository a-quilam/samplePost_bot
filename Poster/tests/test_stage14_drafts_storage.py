# tests/test_stage14_drafts_storage.py
"""
Stage 14, §1: бессрочное хранение черновиков.

По решению пользователя TTL-очистка (30 дней) отключена: черновики
хранятся бессрочно, пока пользователь сам не удалит их. Тесты фиксируют
новую инварианту и регрессии против старого поведения:

- запуск бота не планирует и не выполняет возрастное удаление;
- черновик старше 30 дней переживает рестарт и открывается в редакторе;
- ручное удаление с подтверждением работает;
- автосохранение WIP и отправка готового поста не зависят от TTL.

Только временные SQLite (FakeBot-харнесс Stage 9); настоящая
Poster/post_bot.db не затрагивается.
"""

import unittest

try:
    from datetime import timedelta

    from sqlalchemy import update as sa_update

    from handlers.post_creation import POST_CREATION
    from models import Draft, utcnow
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        goto_preview,
        scenario_test,
    )

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as e:  # noqa: BLE001 — причина попадает в сообщение пропуска
    INTEGRATION_IMPORT_ERROR = str(e)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PerpetualStorageTests(unittest.TestCase):
    """Черновики не удаляются по возрасту; ручное удаление работает."""

    async def _make_draft_via_ui(self, h, title: str = "Черновик навсегда") -> int:
        """Создаёт черновик через интерфейс, возвращает его id."""
        await goto_preview(h, title=title)
        await h.callback("save_draft")
        self.assertEqual(len(h.drafts()), 1)
        return h.drafts()[0].id

    async def _backdate_draft(self, h, draft_id: int, days: int) -> None:
        """Состаривает черновик: created_at давно, updated_at NULL."""
        session = h.session_factory()
        try:
            session.execute(
                sa_update(Draft)
                .where(Draft.id == draft_id)
                .values(created_at=utcnow() - timedelta(days=days), updated_at=None)
            )
            session.commit()
        finally:
            session.close()

    @scenario_test
    async def test_old_draft_survives_restart_and_still_opens(self, h):
        """Черновик старше 30 дней переживает рестарт и открывается."""
        draft_id = await self._make_draft_via_ui(h)
        await self._backdate_draft(h, draft_id, days=40)

        await h.restart()
        await h.text("/start")
        await h.callback("drafts")
        self.assertEqual(
            [d.id for d in h.drafts()],
            [draft_id],
            "возрастной черновик виден в списке (бессрочное хранение)",
        )

        await h.callback(f"editdraft_{draft_id}")
        # Открытие черновика = превью с его содержимым (текст поста)
        self.assertIn("Черновик навсегда", h.posts()[-1], "содержимое открылось")
        self.assertEqual(h.dialog_state(), POST_CREATION)

        # Правка старого черновика обновляет ту же запись — без копий
        await h.callback("save_draft")
        self.assertEqual(len(h.drafts()), 1, "копий не появилось")

    @scenario_test
    async def test_manual_deletion_of_old_draft_works(self, h):
        """Возрастной черновик удаляется пользователем (с подтверждением)."""
        draft_id = await self._make_draft_via_ui(h, title="Под удаление")
        await self._backdate_draft(h, draft_id, days=400)

        await h.text("/start")
        await h.callback("drafts")
        await h.callback(f"delete_{draft_id}")
        self.assertEqual(len(h.drafts()), 1, "без подтверждения не удалён")

        await h.callback(f"confirmdelete_{draft_id}")
        self.assertEqual(h.drafts(), [], "после подтверждения удалён")
        self.assertIn("Черновик 1 удалён.", h.screen_text())

    @scenario_test
    async def test_wip_autosave_works_without_ttl(self, h):
        """Автосохранение WIP (снимок на каждом шаге) не зависит от TTL."""
        await h.text("✏️ Создать пост")
        await h.text("Заголовок после рестарта")

        # Снимок WIP пишется при переходе между шагами — до рестарта
        state = h.user_states()[0]
        self.assertTrue(state.payload, "снимок WIP записан в user_states")

        await h.restart()
        await h.callback("restore_wip")
        self.assertEqual(h.user_data().get("title"), "Заголовок после рестарта")
        self.assertEqual(h.dialog_state(), POST_CREATION, "диалог продолжен")

    @scenario_test
    async def test_start_during_creation_still_saves_draft(self, h):
        """Выход из диалога (/start) сохраняет незавершённый пост — как раньше."""
        await h.text("✏️ Создать пост")
        await h.text("Незавершённый пост")
        await h.text("/start")

        drafts = h.drafts()
        self.assertEqual(len(drafts), 1, "черновик сохранён ровно один раз")
        self.assertEqual(drafts[0].title, "Незавершённый пост")
        self.assertIsNone(h.dialog_state(), "диалог завершён")

    @scenario_test
    async def test_finish_still_sends_post(self, h):
        """Готовый пост отправляется как раньше (регрессия §7.6)."""
        await goto_preview(h, title="Готовый пост Stage 14")
        await h.callback("finish")

        self.assertIn("Пост готов.", h.screen_text())
        self.assertEqual(len(h.post_records()), 2, "превью + готовый пост")
        self.assertEqual(h.dialog_state(), None, "цикл завершён")
        self.assertEqual(h.drafts(), [], "пост не скопирован в черновики")


try:
    import importlib.util

    import bot as bot_module

    JOBS_IMPORT_ERROR = None
except Exception as e:  # noqa: BLE001
    JOBS_IMPORT_ERROR = str(e)


@unittest.skipIf(
    JOBS_IMPORT_ERROR is not None, f"импорт недоступен: {JOBS_IMPORT_ERROR}"
)
class NoTtlJobTests(unittest.TestCase):
    """Задача remove_old_drafts больше не регистрируется (§1.2, §7.4)."""

    def test_build_application_schedules_no_jobs(self):
        app = bot_module.build_application()
        job_queue = app.job_queue
        jobs = list(job_queue.jobs()) if job_queue is not None else []
        names = [job.name for job in jobs]
        self.assertNotIn(
            "remove_old_drafts",
            names,
            "TTL-задача не должна планироваться при запуске",
        )
        self.assertEqual(jobs, [], "фоновых задач в приложении нет вовсе")

    def test_jobs_module_fully_removed(self):
        self.assertIsNone(
            importlib.util.find_spec("handlers.jobs"),
            "мёртвый модуль TTL-очистки должен быть удалён",
        )


if __name__ == "__main__":
    unittest.main()
