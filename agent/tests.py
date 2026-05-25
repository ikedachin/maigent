import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from .access import is_path_allowed, normalize_access_path
from .config import RuntimeConfig, load_runtime_config
from .openai_client import stream_response
from .models import AgentRun, AgentTaskRecord, AppSetting, ApprovalRequest, FeatureFlag, Message, Project, ProjectAccessPath, Thread
from .tooling import (
    AgentPlan,
    AgentPlanStep,
    SandboxResult,
    _python_for_tabular_task,
    build_agent_plan,
    can_build_sandbox_program,
    requires_llm_sandbox_program,
    run_sandbox,
    select_tool,
)
from .views import _avoid_failed_plan
from .views import AgentState, TaskExecutionRecord, _replan_after_step, _route_final_output


@override_settings(STATICFILES_DIRS=[])
class ConfigTests(TestCase):
    def test_app_config_overrides_user_config(self):
        with TemporaryDirectory() as home_dir, TemporaryDirectory() as app_dir:
            home = Path(home_dir)
            app = Path(app_dir)
            (home / ".maigent").mkdir()
            (app / ".maigent").mkdir()
            (home / ".maigent" / "config.toml").write_text('model = "user-model"\napi_key = "user-key"\n')
            (app / ".maigent" / "config.toml").write_text('model = "app-model"\nbase_url = "http://local"\n')

            with patch("agent.config.Path.home", return_value=home), override_settings(BASE_DIR=app):
                config = load_runtime_config("")

            self.assertEqual(config.model, "app-model")
            self.assertEqual(config.api_key, "user-key")
            self.assertEqual(config.base_url, "http://local")
            self.assertEqual(len(config.sources), 2)

    def test_project_config_overrides_app_config(self):
        with TemporaryDirectory() as home_dir, TemporaryDirectory() as app_dir, TemporaryDirectory() as project_dir:
            home = Path(home_dir)
            app = Path(app_dir)
            project = Path(project_dir)
            (app / ".maigent").mkdir()
            (project / ".maigent").mkdir()
            (app / ".maigent" / "config.toml").write_text('model = "app-model"\napi_key = "app-key"\n')
            (project / ".maigent" / "config.toml").write_text('model = "project-model"\nbase_url = "http://project"\n')

            with patch("agent.config.Path.home", return_value=home), override_settings(BASE_DIR=app):
                config = load_runtime_config(str(project))

            self.assertEqual(config.model, "project-model")
            self.assertEqual(config.api_key, "app-key")
            self.assertEqual(config.base_url, "http://project")
            self.assertEqual(len(config.sources), 2)

    def test_api_mode_defaults_to_auto_and_accepts_chat(self):
        with TemporaryDirectory() as app_dir:
            app = Path(app_dir)
            (app / ".maigent").mkdir()
            (app / ".maigent" / "config.toml").write_text('model = "m"\napi_mode = "chat"\n')

            with override_settings(BASE_DIR=app):
                config = load_runtime_config("")

            self.assertEqual(config.api_mode, "chat")

    def test_provider_config_selects_first_enabled_provider(self):
        config = RuntimeConfig(
            values={
                "providers": {
                    "openai": {"enabled": False, "model": "gpt"},
                    "ollama": {"enabled": True, "model": "llama3.1"},
                    "openrouter": {"enabled": True, "model": "openai/gpt-4o-mini", "api_key": "router-key"},
                }
            },
            sources=[],
        )

        self.assertEqual(config.active_provider, "ollama")
        self.assertEqual(config.model, "llama3.1")
        self.assertEqual(config.base_url, "http://localhost:11434/v1")
        self.assertEqual(config.api_mode, "chat")
        self.assertEqual(config.api_key, "")

    def test_openrouter_provider_uses_provider_api_key_and_redacts_it(self):
        config = RuntimeConfig(
            values={
                "providers": {
                    "openrouter": {
                        "enabled": True,
                        "model": "openai/gpt-4o-mini",
                        "api_key": "router-key",
                    }
                }
            },
            sources=[],
        )

        self.assertEqual(config.active_provider, "openrouter")
        self.assertEqual(config.api_key, "router-key")
        self.assertEqual(config.base_url, "https://openrouter.ai/api/v1")
        self.assertEqual(config.redacted()["providers"]["openrouter"]["api_key"], "********")

    def test_bedrock_provider_exposes_credentials_and_redacts_them(self):
        config = RuntimeConfig(
            values={
                "providers": {
                    "bedrock": {
                        "enabled": True,
                        "model": "anthropic.model",
                        "region": "ap-northeast-1",
                        "aws_access_key_id": "access",
                        "aws_secret_access_key": "secret",
                        "aws_session_token": "token",
                    }
                }
            },
            sources=[],
        )

        self.assertEqual(
            config.bedrock_credentials,
            {
                "aws_access_key_id": "access",
                "aws_secret_access_key": "secret",
                "aws_session_token": "token",
            },
        )
        safe = config.redacted()["providers"]["bedrock"]
        self.assertEqual(safe["aws_access_key_id"], "********")
        self.assertEqual(safe["aws_secret_access_key"], "********")
        self.assertEqual(safe["aws_session_token"], "********")

    def test_all_providers_disabled_disables_model_resolution(self):
        config = RuntimeConfig(
            values={
                "model": "legacy-model",
                "providers": {
                    "openai": {"enabled": False, "model": "gpt"},
                    "bedrock": {"enabled": False, "model": "anthropic.model"},
                },
            },
            sources=[],
        )

        self.assertEqual(config.active_provider, "")
        self.assertEqual(config.model, "")

    def test_yaml_config_loads_tools_and_sandbox_libraries(self):
        with TemporaryDirectory() as app_dir:
            app = Path(app_dir)
            (app / ".maigent").mkdir()
            (app / ".maigent" / "config.yaml").write_text(
                "\n".join(
                    [
                        "tools:",
                        "  rag:",
                        "    enabled: true",
                        "  sandbox:",
                        "    enabled: true",
                        "    image: python:3.12-slim",
                        "    timeout_seconds: 300",
                        "    install_libraries_on_run: true",
                        "    allowed_libraries:",
                        "      - numpy",
                        "      - pandas",
                    ]
                ),
                encoding="utf-8",
            )

            with override_settings(BASE_DIR=app):
                config = load_runtime_config("")

            self.assertTrue(config.tool_enabled("sandbox"))
            self.assertEqual(config.sandbox_image, "python:3.12-slim")
            self.assertEqual(config.sandbox_timeout_seconds, 300)
            self.assertTrue(config.sandbox_install_libraries_on_run)
            self.assertEqual(config.sandbox_allowed_libraries, ["numpy", "pandas"])

    def test_final_evaluation_config_clamps_retries(self):
        with TemporaryDirectory() as app_dir:
            app = Path(app_dir)
            (app / ".maigent").mkdir()
            (app / ".maigent" / "config.toml").write_text(
                "\n".join(
                    [
                        "[final_evaluation]",
                        "enabled = true",
                        "max_retries = 9",
                    ]
                ),
                encoding="utf-8",
            )

            with override_settings(BASE_DIR=app):
                config = load_runtime_config("")

            self.assertTrue(config.final_evaluation_enabled)
            self.assertEqual(config.final_evaluation_max_retries, 3)


@override_settings(STATICFILES_DIRS=[])
class ChatFlowTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Repo", path="")
        self.thread = Thread.objects.create(project=self.project, title="Main")

    @patch("agent.views.load_runtime_config")
    def test_model_missing_returns_error_without_stream_url(self, mock_config):
        mock_config.return_value.model = ""
        mock_config.return_value.api_key = ""
        mock_config.return_value.base_url = ""
        mock_config.return_value.sources = []

        response = self.client.post(reverse("send_message", args=[self.thread.id]), {"message": "hello"})

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertTrue(payload["error"])
        self.assertNotIn("stream_url", payload)
        self.assertEqual(Message.objects.filter(thread=self.thread).count(), 2)
        self.assertEqual(Message.objects.filter(role="assistant").first().status, "error")

    def test_update_rag_settings_clamps_top_k(self):
        response = self.client.post(
            reverse("update_rag_settings"),
            {
                "rag_top_k": "99",
                "final_evaluation_enabled": "on",
                "final_evaluation_max_retries": "9",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(AppSetting.objects.get(key="rag_top_k").value, "10")
        self.assertEqual(AppSetting.objects.get(key="final_evaluation_enabled").value, "true")
        self.assertEqual(AppSetting.objects.get(key="final_evaluation_max_retries").value, "3")

    def test_dashboard_creates_builtin_file_write_flag(self):
        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        flag = FeatureFlag.objects.get(name="file_write")
        self.assertTrue(flag.enabled)

    def test_update_feature_flag_toggles_flag(self):
        flag = FeatureFlag.objects.create(name="file_write", enabled=True)

        response = self.client.post(reverse("update_feature_flag", args=[flag.id]), {"action": "disable"})

        self.assertEqual(response.status_code, 302)
        flag.refresh_from_db()
        self.assertFalse(flag.enabled)

    def test_slash_status_returns_assistant_message(self):
        FeatureFlag.objects.create(name="web_search", enabled=True)
        response = self.client.post(reverse("send_message", args=[self.thread.id]), {"message": "/status"})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["command"])
        self.assertIn("Project: Repo", payload["content"])
        self.assertIn("web_search=on", payload["content"])

    def test_features_command_toggles_flag(self):
        response = self.client.post(reverse("send_message", args=[self.thread.id]), {"message": "/features enable beta"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(FeatureFlag.objects.get(name="beta").enabled)

    def test_memories_command_toggles_thread(self):
        response = self.client.post(reverse("send_message", args=[self.thread.id]), {"message": "/memories"})

        self.assertEqual(response.status_code, 200)
        self.thread.refresh_from_db()
        self.assertTrue(self.thread.memory_enabled)

    def test_read_command_reads_allowed_file(self):
        with TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            file_path = root / "allowed.txt"
            file_path.write_text("hello file", encoding="utf-8")
            ProjectAccessPath.objects.create(project=self.project, path=str(root), mode="read")

            response = self.client.post(reverse("send_message", args=[self.thread.id]), {"message": f"/read {file_path}"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("hello file", response.json()["content"])

    def test_read_command_rejects_unallowed_file(self):
        with TemporaryDirectory() as root_dir:
            file_path = Path(root_dir) / "blocked.txt"
            file_path.write_text("secret", encoding="utf-8")

            response = self.client.post(reverse("send_message", args=[self.thread.id]), {"message": f"/read {file_path}"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("読み取りが許可されていません", response.json()["content"])

    def test_ls_command_lists_allowed_folder(self):
        with TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            (root / "a.txt").write_text("a", encoding="utf-8")
            (root / "sub").mkdir()
            ProjectAccessPath.objects.create(project=self.project, path=str(root), mode="read")

            response = self.client.post(reverse("send_message", args=[self.thread.id]), {"message": f"/ls {root}"})

        self.assertEqual(response.status_code, 200)
        content = response.json()["content"]
        self.assertIn("[dir] sub", content)
        self.assertIn("[file] a.txt", content)

    def test_file_command_without_path_lists_allowed_paths(self):
        ProjectAccessPath.objects.create(project=self.project, path="/tmp/example", mode="read")

        response = self.client.post(reverse("send_message", args=[self.thread.id]), {"message": "/file"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("許可済みファイル/フォルダ", response.json()["content"])
        self.assertIn("/tmp/example", response.json()["content"])

    def test_file_command_reads_file_or_lists_folder(self):
        with TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            file_path = root / "note.txt"
            file_path.write_text("note body", encoding="utf-8")
            ProjectAccessPath.objects.create(project=self.project, path=str(root), mode="read")

            file_response = self.client.post(reverse("send_message", args=[self.thread.id]), {"message": f"/file {file_path}"})
            folder_response = self.client.post(reverse("send_message", args=[self.thread.id]), {"message": f"/file {root}"})

        self.assertIn("note body", file_response.json()["content"])
        self.assertIn("[file] note.txt", folder_response.json()["content"])

    def test_write_command_writes_allowed_file(self):
        with TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            file_path = root / "created.txt"
            ProjectAccessPath.objects.create(project=self.project, path=str(root), mode="write")

            response = self.client.post(
                reverse("send_message", args=[self.thread.id]),
                {"message": f"/write {file_path} -- hello write"},
            )

            self.assertEqual(response.status_code, 200)
            self.assertIn("書き込みしました", response.json()["content"])
            self.assertEqual(file_path.read_text(encoding="utf-8"), "hello write")

    def test_write_command_rejects_disabled_file_write_flag(self):
        FeatureFlag.objects.create(name="file_write", enabled=False)
        with TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            file_path = root / "blocked.txt"
            ProjectAccessPath.objects.create(project=self.project, path=str(root), mode="write")

            response = self.client.post(
                reverse("send_message", args=[self.thread.id]),
                {"message": f"/write {file_path} -- blocked"},
            )

            self.assertEqual(response.status_code, 200)
            self.assertIn("ファイル書き込み機能が無効です", response.json()["content"])
            self.assertFalse(file_path.exists())

    def test_write_command_rejects_read_only_access(self):
        with TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            file_path = root / "blocked.txt"
            ProjectAccessPath.objects.create(project=self.project, path=str(root), mode="read")

            response = self.client.post(
                reverse("send_message", args=[self.thread.id]),
                {"message": f"/write {file_path} -- blocked"},
            )

            self.assertEqual(response.status_code, 200)
            self.assertIn("書き込みが許可されていません", response.json()["content"])
            self.assertFalse(file_path.exists())

    def test_append_command_appends_allowed_file(self):
        with TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            file_path = root / "note.txt"
            file_path.write_text("first", encoding="utf-8")
            ProjectAccessPath.objects.create(project=self.project, path=str(root), mode="write")

            response = self.client.post(
                reverse("send_message", args=[self.thread.id]),
                {"message": f"/append {file_path} -- second"},
            )

            self.assertEqual(response.status_code, 200)
            self.assertIn("追記しました", response.json()["content"])
            self.assertEqual(file_path.read_text(encoding="utf-8"), "firstsecond")

    def test_fork_command_copies_messages(self):
        Message.objects.create(thread=self.thread, role="user", content="before")
        response = self.client.post(reverse("send_message", args=[self.thread.id]), {"message": "/fork"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Thread.objects.count(), 2)
        fork = Thread.objects.exclude(id=self.thread.id).get()
        self.assertGreaterEqual(fork.messages.count(), 2)

    def test_delete_thread_redirects_to_remaining_thread(self):
        other = Thread.objects.create(project=self.project, title="Other")

        response = self.client.post(reverse("delete_thread", args=[self.thread.id]))

        self.assertEqual(response.status_code, 302)
        self.assertFalse(Thread.objects.filter(id=self.thread.id).exists())
        self.assertEqual(response.url, reverse("thread", args=[other.id]))

    def test_delete_last_thread_recreates_main_thread(self):
        response = self.client.post(reverse("delete_thread", args=[self.thread.id]))

        self.assertEqual(response.status_code, 302)
        replacement = Thread.objects.get(project=self.project)
        self.assertEqual(replacement.title, "Main thread")
        self.assertEqual(response.url, reverse("thread", args=[replacement.id]))

    @patch("agent.views.stream_response")
    @patch("agent.views.load_runtime_config")
    def test_streaming_saves_complete_assistant_message(self, mock_config, mock_stream):
        mock_config.return_value.model = "test-model"
        mock_config.return_value.api_key = "test-key"
        mock_config.return_value.base_url = ""
        mock_config.return_value.sources = []
        mock_stream.return_value = iter([("delta", "hel"), ("delta", "lo"), ("response_id", "resp_1")])

        create = self.client.post(reverse("send_message", args=[self.thread.id]), {"message": "hello"})
        self.assertEqual(create.status_code, 200)
        payload = create.json()

        stream = self.client.get(payload["stream_url"])
        body = b"".join(stream.streaming_content).decode()

        self.assertIn('"progress_tail"', body)
        self.assertIn("Goal: Answer the user's request: hello", body)
        self.assertIn("Criteria: The answer directly addresses the user's request.", body)
        self.assertIn("Plan: Direct answer; no tool use needed.", body)
        self.assertIn('"done": true', body)
        assistant = Message.objects.get(id=payload["assistant_id"])
        self.assertEqual(assistant.content, "hello")
        self.assertEqual(assistant.status, "complete")
        self.assertEqual(assistant.openai_response_id, "resp_1")
        run = AgentRun.objects.get(assistant_message=assistant)
        self.assertEqual(run.status, "complete")
        self.assertEqual(run.user_message_id, payload["user_id"])
        self.assertEqual(run.goal, "Answer the user's request: hello")
        self.assertEqual(run.current_plan_queue, [])
        self.assertEqual(run.final_message, "hello")

    @patch("agent.views.complete_response")
    @patch("agent.views.stream_response")
    @patch("agent.views.load_runtime_config")
    def test_final_evaluation_retries_from_plan_when_inadequate(self, mock_config, mock_stream, mock_complete):
        class Config:
            model = "test-model"
            api_key = "test-key"
            base_url = ""
            sources = []
            final_evaluation_enabled = True
            final_evaluation_max_retries = 1

            def tool_enabled(self, name, default=False):
                return False

        mock_config.return_value = Config()
        mock_stream.side_effect = [
            iter([("delta", "bad answer"), ("response_id", "resp_1")]),
            iter([("delta", "good answer"), ("response_id", "resp_2")]),
        ]
        mock_complete.side_effect = [
            '{"adequate": false, "reason": "too vague"}',
            '{"adequate": true, "reason": "answers the question"}',
        ]

        create = self.client.post(reverse("send_message", args=[self.thread.id]), {"message": "hello"})
        payload = create.json()
        stream = self.client.get(payload["stream_url"])
        body = b"".join(stream.streaming_content).decode()

        self.assertIn("good answer", body)
        self.assertIn("final evaluation failed; replanning with a different plan", body)
        self.assertIn("Revised direct answer after failed final evaluation", body)
        assistant = Message.objects.get(id=payload["assistant_id"])
        self.assertEqual(assistant.content, "good answer")
        self.assertEqual(assistant.openai_response_id, "resp_2")
        self.assertEqual(mock_stream.call_count, 2)
        first_evaluation_prompt = mock_complete.call_args_list[0].args[1]
        self.assertIn("Goal set before planning:", first_evaluation_prompt)
        self.assertIn("Evaluation criteria set before planning:", first_evaluation_prompt)
        self.assertIn("The answer directly addresses the user's request.", first_evaluation_prompt)

    def test_failed_final_evaluation_uses_different_plan(self):
        class Config:
            def tool_enabled(self, name, default=False):
                return name == "rag" or default

        plan = AgentPlan(
            goal="Answer the user's request: hello",
            evaluation_criteria=["The answer directly addresses the user's request."],
            summary="Direct answer; no tool use needed.",
            steps=[AgentPlanStep("final", "Answer directly.")],
        )
        revised = _avoid_failed_plan(plan, Config(), [plan.summary])

        self.assertNotEqual(revised.summary, plan.summary)
        self.assertEqual(revised.goal, plan.goal)
        self.assertEqual(revised.evaluation_criteria, plan.evaluation_criteria)
        self.assertEqual([step.tool for step in revised.steps], ["rag", "final"])

    def test_failed_sandbox_plan_retries_without_sandbox(self):
        class Config:
            def tool_enabled(self, name, default=False):
                return True

        plan = AgentPlan(
            goal="Answer the user's request: calculate from files",
            evaluation_criteria=["The answer includes the computed result."],
            summary="rag -> sandbox -> final",
            steps=[
                AgentPlanStep("rag", "Search context."),
                AgentPlanStep("sandbox", "Run calculation."),
            ],
        )
        revised = _avoid_failed_plan(plan, Config(), [plan.summary])

        self.assertNotEqual(revised.summary, plan.summary)
        self.assertEqual(revised.goal, plan.goal)
        self.assertEqual(revised.evaluation_criteria, plan.evaluation_criteria)
        self.assertEqual([step.tool for step in revised.steps], ["rag"])

    @patch("agent.views.run_sandbox")
    @patch("agent.views.load_runtime_config")
    def test_streaming_uses_sandbox_tool_when_selected(self, mock_config, mock_run_sandbox):
        class Config:
            model = "test-model"
            api_key = "test-key"
            base_url = ""
            sources = []
            sandbox_allowed_libraries = []
            sandbox_image = "python:3.11-slim"
            sandbox_timeout_seconds = 20

            def tool_enabled(self, name, default=False):
                return name == "sandbox"

        mock_config.return_value = Config()
        mock_run_sandbox.return_value = SandboxResult(True, "4")

        create = self.client.post(reverse("send_message", args=[self.thread.id]), {"message": "2 + 2を正確に計算して"})
        payload = create.json()
        stream = self.client.get(payload["stream_url"])
        b"".join(stream.streaming_content)

        assistant = Message.objects.get(id=payload["assistant_id"])
        self.assertIn("Sandbox実行結果: 成功", assistant.content)
        self.assertIn("4", assistant.content)
        mock_run_sandbox.assert_called_once()
        run = AgentRun.objects.get(assistant_message=assistant)
        self.assertEqual(run.status, "complete")
        self.assertEqual(run.final_message, assistant.content)
        task = AgentTaskRecord.objects.get(run=run)
        self.assertEqual(task.sequence, 1)
        self.assertEqual(task.tool, "sandbox")
        self.assertEqual(task.status, "ok")
        self.assertIn("4", task.result)

    @patch("agent.tooling.subprocess.run")
    def test_sandbox_generates_program_for_natural_language_sum(self, mock_run):
        class Config:
            sandbox_allowed_libraries = []
            sandbox_image = "python:3.11-slim"
            sandbox_timeout_seconds = 20

        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "6\n"
        mock_run.return_value.stderr = ""

        result = run_sandbox("1, 2, 3 の合計を正確に計算して", Config())

        self.assertTrue(result.ok)
        command = mock_run.call_args.args[0]
        self.assertEqual(command[-1], "python /work/script.py")

    @patch("agent.tooling.subprocess.run")
    def test_sandbox_does_not_install_libraries_on_each_run_by_default(self, mock_run):
        class Config:
            sandbox_allowed_libraries = ["numpy", "pandas"]
            sandbox_install_libraries_on_run = False
            sandbox_image = "maigent-sandbox:py311"
            sandbox_timeout_seconds = 20

        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "6\n"
        mock_run.return_value.stderr = ""

        result = run_sandbox("1, 2, 3 の合計を正確に計算して", Config())

        self.assertTrue(result.ok)
        command = mock_run.call_args.args[0]
        self.assertIn("--network", command)
        self.assertNotIn("pip install", command[-1])

    @patch("agent.tooling.subprocess.run")
    def test_sandbox_generates_program_from_rag_numeric_context(self, mock_run):
        class Config:
            sandbox_allowed_libraries = []
            sandbox_image = "python:3.11-slim"
            sandbox_timeout_seconds = 20

        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "20\n"
        mock_run.return_value.stderr = ""
        message = "\n".join(
            [
                "Agent plan: rag -> sandbox -> final",
                "RAG context from allowed local files:",
                "--- numbers.txt ---",
                "5",
                "15",
                "",
                "numbersファイルの平均を正確に計算して",
            ]
        )

        result = run_sandbox(message, Config())

        self.assertTrue(result.ok)
        self.assertEqual(result.output, "20")
        mock_run.assert_called_once()

    def test_sandbox_generates_csv_program_for_score_column(self):
        message = "\n".join(
            [
                "test.csvファイルの内容を確認して、テストの点の平均点と標準偏差を実際にプログラムを実行して回答してください。",
                "",
                "RAG context from allowed local files:",
                "Auto-selected file: /tmp/test.csv",
                "```text",
                "id,name,テストの点",
                "1001,Alice,70",
                "1002,Bob,80",
                "1003,Carol,90",
                "```",
            ]
        )

        code = _python_for_tabular_task(message)
        namespace = {"__name__": "__main__"}
        with patch("builtins.print") as mock_print:
            exec(code, namespace)

        printed = "\n".join(str(call.args[0]) for call in mock_print.call_args_list)
        self.assertIn("column: テストの点", printed)
        self.assertIn("mean: 80", printed)
        self.assertIn("population_std:", printed)
        self.assertNotIn("column: id", printed)

    @patch("agent.tooling.subprocess.run")
    def test_sandbox_prefers_csv_program_over_first_number(self, mock_run):
        class Config:
            sandbox_allowed_libraries = []
            sandbox_image = "python:3.11-slim"
            sandbox_timeout_seconds = 20

        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "column: テストの点\nmean: 80\n"
        mock_run.return_value.stderr = ""
        message = "\n".join(
            [
                "test.csvファイルの内容を確認して、テストの点の平均点と標準偏差を実際にプログラムを実行して回答してください。",
                "",
                "RAG context from allowed local files:",
                "Auto-selected file: /tmp/test.csv",
                "```text",
                "id,name,テストの点",
                "1001,Alice,70",
                "1002,Bob,80",
                "1003,Carol,90",
                "```",
            ]
        )

        result = run_sandbox(message, Config())

        self.assertTrue(result.ok)
        command = mock_run.call_args.args[0]
        self.assertIn("python /work/script.py", command[-1])
        self.assertIn("column: テストの点", result.output)

    def test_sandbox_csv_program_outputs_sum_for_add_up_request(self):
        message = "\n".join(
            [
                "テストの点を全て足し合わせてください。ファイルはtest.csvです。",
                "",
                "RAG context from allowed local files:",
                "Auto-selected file: /tmp/test.csv",
                "```text",
                "名前,テストの種類,テストの点",
                "佐藤太郎,国語,82",
                "佐藤太郎,数学,76",
                "佐藤太郎,英語,88",
                "```",
            ]
        )

        code = _python_for_tabular_task(message)
        namespace = {"__name__": "__main__"}
        with patch("builtins.print") as mock_print:
            exec(code, namespace)

        printed = "\n".join(str(call.args[0]) for call in mock_print.call_args_list)
        self.assertIn("column: テストの点", printed)
        self.assertIn("sum: 246", printed)

    def test_sandbox_csv_program_outputs_mean_and_variance(self):
        message = "\n".join(
            [
                "test.csvの全てのテストの点の平均と分散を求めてください。",
                "",
                "RAG context from allowed local files:",
                "Auto-selected file: /tmp/test.csv",
                "```text",
                "名前,テストの点",
                "A,70",
                "B,80",
                "C,90",
                "```",
            ]
        )

        code = _python_for_tabular_task(message)
        namespace = {"__name__": "__main__"}
        with patch("builtins.print") as mock_print:
            exec(code, namespace)

        printed = "\n".join(str(call.args[0]) for call in mock_print.call_args_list)
        self.assertIn("column: テストの点", printed)
        self.assertIn("mean: 80", printed)
        self.assertIn("population_variance:", printed)
        self.assertIn("sample_variance:", printed)

    def test_sandbox_csv_program_outputs_text_histogram(self):
        message = "\n".join(
            [
                "test.csvのテストの点のヒストグラムを作ってください。",
                "",
                "RAG context from allowed local files:",
                "Auto-selected file: /tmp/test.csv",
                "```text",
                "名前,テストの点",
                "A,70",
                "B,80",
                "C,90",
                "```",
            ]
        )

        code = _python_for_tabular_task(message)
        namespace = {"__name__": "__main__"}
        with patch("builtins.print") as mock_print:
            exec(code, namespace)

        printed = "\n".join(str(call.args[0]) for call in mock_print.call_args_list)
        self.assertIn("column: テストの点", printed)
        self.assertIn("histogram:", printed)

    def test_grouped_csv_task_requires_llm_generated_program(self):
        message = "\n".join(
            [
                "test.csvのテストの点を名前ごとに全ての科目を合計してください。",
                "",
                "RAG context from allowed local files:",
                "Auto-selected file: /tmp/test.csv",
                "```text",
                "名前,テストの種類,テストの点",
                "佐藤太郎,国語,82",
                "佐藤太郎,数学,76",
                "```",
            ]
        )

        self.assertTrue(requires_llm_sandbox_program(message))
        self.assertFalse(can_build_sandbox_program(message))

    @patch("agent.views.stream_response")
    @patch("agent.views.run_sandbox")
    @patch("agent.views.load_runtime_config")
    def test_sandbox_no_code_falls_back_to_llm(self, mock_config, mock_run_sandbox, mock_stream):
        class Config:
            model = "test-model"
            api_key = "test-key"
            base_url = ""
            sources = []
            sandbox_allowed_libraries = []
            sandbox_image = "python:3.11-slim"
            sandbox_timeout_seconds = 20

            def tool_enabled(self, name, default=False):
                return name == "sandbox"

        mock_config.return_value = Config()
        mock_run_sandbox.return_value = SandboxResult(False, "sandboxで実行できるPythonコードまたは計算式を特定できませんでした。")
        mock_stream.return_value = iter([("delta", "通常回答")])

        create = self.client.post(reverse("send_message", args=[self.thread.id]), {"message": "正確に説明して"})
        payload = create.json()
        stream = self.client.get(payload["stream_url"])
        b"".join(stream.streaming_content)

        assistant = Message.objects.get(id=payload["assistant_id"])
        self.assertEqual(assistant.content, "通常回答")
        mock_run_sandbox.assert_not_called()
        mock_stream.assert_called_once()

    def test_precise_explanation_does_not_select_sandbox_without_program(self):
        class Config:
            def tool_enabled(self, name, default=False):
                return name == "sandbox"

        plan = build_agent_plan("正確に説明してください", Config())

        self.assertEqual(plan.goal, "Answer the user's request: 正確に説明してください")
        self.assertIn("The answer directly addresses the user's request.", plan.evaluation_criteria)
        self.assertEqual([step.tool for step in plan.steps], ["final"])
        self.assertFalse(can_build_sandbox_program("正確に説明してください"))

    def test_tool_selection_prefers_sandbox_for_calculation_when_enabled(self):
        class Config:
            def tool_enabled(self, name, default=False):
                return name == "sandbox" or (name == "rag" and default)

        decision = select_tool("123 * 456 を正確に計算して", Config())

        self.assertEqual(decision.name, "sandbox")

    def test_agent_plan_can_include_rag_then_sandbox(self):
        class Config:
            def tool_enabled(self, name, default=False):
                return name in {"rag", "sandbox"}

        plan = build_agent_plan("ファイルの数を正確に計算して", Config())

        self.assertEqual([step.tool for step in plan.steps], ["rag", "sandbox"])

    def test_agent_plan_uses_rag_before_sandbox_for_named_csv(self):
        class Config:
            def tool_enabled(self, name, default=False):
                return name in {"rag", "sandbox"}

        plan = build_agent_plan("test.csvの全てのテストの点を足し合わせてください。", Config())

        self.assertEqual([step.tool for step in plan.steps], ["rag", "sandbox"])

    @patch("agent.views.complete_response")
    def test_dynamic_replanner_can_replace_remaining_queue(self, mock_complete):
        class Config:
            def tool_enabled(self, name, default=False):
                return name == "dynamic_replanner"

        state = AgentState.from_plan(
            AgentPlan(
                goal="Answer the user's request: calculate",
                evaluation_criteria=["The answer includes the computed result."],
                summary="rag -> sandbox -> final",
                steps=[AgentPlanStep("sandbox", "Run calculation.")],
            ),
            "calculate",
        )
        state.task_history.append(
            TaskExecutionRecord(
                task=AgentPlanStep("rag", "Search context."),
                ok=True,
                input_before="calculate",
                input_after="calculate with context",
                result="completed",
            )
        )
        mock_complete.return_value = json.dumps(
            {
                "action": "replace",
                "reason": "Need debugging before sandbox.",
                "tasks": [{"tool": "sandbox", "purpose": "Run a narrower debug calculation."}],
            }
        )

        decision = _replan_after_step(Config(), state)

        self.assertEqual(decision.action, "replace")
        self.assertEqual([step.tool for step in decision.plan_queue], ["sandbox"])
        self.assertIn("debug", decision.plan_queue[0].purpose)

    @patch("agent.views.complete_response")
    def test_dynamic_finalizer_can_add_validation_tasks(self, mock_complete):
        class Config:
            def tool_enabled(self, name, default=False):
                return name == "dynamic_finalizer"

        state = AgentState.from_plan(
            AgentPlan(
                goal="Answer the user's request: verify",
                evaluation_criteria=["The answer is verified."],
                summary="sandbox -> final",
                steps=[],
            ),
            "verify",
        )
        state.final_message = "temporary artifact"
        mock_complete.return_value = json.dumps(
            {
                "action": "add_tasks",
                "reason": "Needs one more validation.",
                "tasks": [{"tool": "sandbox", "purpose": "Validate the artifact."}],
            }
        )

        decision = _route_final_output(Config(), state)

        self.assertEqual(decision.action, "replace")
        self.assertEqual([step.tool for step in decision.plan_queue], ["sandbox"])

    @patch("agent.views.stream_response")
    @patch("agent.views.load_runtime_config")
    def test_streaming_attaches_allowed_file_context_from_plain_message(self, mock_config, mock_stream):
        mock_config.return_value.model = "test-model"
        mock_config.return_value.api_key = "test-key"
        mock_config.return_value.base_url = ""
        mock_config.return_value.sources = []
        mock_stream.return_value = iter([("delta", "ok")])
        with TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            file_path = root / "note.txt"
            file_path.write_text("allowed context", encoding="utf-8")
            ProjectAccessPath.objects.create(project=self.project, path=str(root), mode="read")

            create = self.client.post(reverse("send_message", args=[self.thread.id]), {"message": f"このファイルを読んで {file_path}"})
            payload = create.json()
            stream = self.client.get(payload["stream_url"])
            b"".join(stream.streaming_content)

        input_text = mock_stream.call_args.args[1]
        self.assertIn("RAG context from allowed local files", input_text)
        self.assertIn("allowed context", input_text)

    @patch("agent.views.stream_response")
    @patch("agent.views.load_runtime_config")
    def test_streaming_does_not_attach_unallowed_file_context(self, mock_config, mock_stream):
        mock_config.return_value.model = "test-model"
        mock_config.return_value.api_key = "test-key"
        mock_config.return_value.base_url = ""
        mock_config.return_value.sources = []
        mock_stream.return_value = iter([("delta", "ok")])
        with TemporaryDirectory() as root_dir:
            file_path = Path(root_dir) / "secret.txt"
            file_path.write_text("secret context", encoding="utf-8")

            create = self.client.post(reverse("send_message", args=[self.thread.id]), {"message": f"このファイルを読んで {file_path}"})
            payload = create.json()
            stream = self.client.get(payload["stream_url"])
            b"".join(stream.streaming_content)

        assistant = Message.objects.get(id=payload["assistant_id"])
        self.assertIn("十分な情報が見つかりません", assistant.content)
        mock_stream.assert_not_called()

    @patch("agent.views.stream_response")
    @patch("agent.views.load_runtime_config")
    def test_streaming_auto_attaches_relevant_allowed_file_without_path(self, mock_config, mock_stream):
        mock_config.return_value.model = "test-model"
        mock_config.return_value.api_key = "test-key"
        mock_config.return_value.base_url = ""
        mock_config.return_value.sources = []
        mock_stream.return_value = iter([("delta", "ok")])
        with TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            (root / "fantasy_story.txt").write_text("dragon castle forest", encoding="utf-8")
            (root / "budget.csv").write_text("amount,total", encoding="utf-8")
            ProjectAccessPath.objects.create(project=self.project, path=str(root), mode="read")

            create = self.client.post(reverse("send_message", args=[self.thread.id]), {"message": "fantasy storyを要約して"})
            payload = create.json()
            stream = self.client.get(payload["stream_url"])
            b"".join(stream.streaming_content)

        input_text = mock_stream.call_args.args[1]
        self.assertIn("Agent plan:", input_text)
        self.assertIn("RAG search query: fantasy story", input_text)
        self.assertIn("RAG context from allowed local files", input_text)
        self.assertIn("fantasy_story.txt", input_text)
        self.assertIn("dragon castle forest", input_text)
        self.assertNotIn("budget.csv", input_text)

    @patch("agent.views.stream_response")
    @patch("agent.views.load_runtime_config")
    def test_streaming_uses_rag_top_k_for_bm25(self, mock_config, mock_stream):
        mock_config.return_value.model = "test-model"
        mock_config.return_value.api_key = "test-key"
        mock_config.return_value.base_url = ""
        mock_config.return_value.sources = []
        mock_stream.return_value = iter([("delta", "ok")])
        AppSetting.objects.create(key="rag_top_k", value="1")
        with TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            (root / "alpha_one.txt").write_text("alpha alpha", encoding="utf-8")
            (root / "alpha_two.txt").write_text("alpha", encoding="utf-8")
            ProjectAccessPath.objects.create(project=self.project, path=str(root), mode="read")

            create = self.client.post(reverse("send_message", args=[self.thread.id]), {"message": "alphaについて要約して"})
            payload = create.json()
            stream = self.client.get(payload["stream_url"])
            b"".join(stream.streaming_content)

        input_text = mock_stream.call_args.args[1]
        self.assertIn("alpha_one.txt", input_text)
        self.assertNotIn("alpha_two.txt", input_text)

    @patch("agent.views.complete_response")
    @patch("agent.views.stream_response")
    @patch("agent.views.load_runtime_config")
    def test_llm_decision_can_select_rag_for_ambiguous_named_question(self, mock_config, mock_stream, mock_complete):
        mock_config.return_value.model = "test-model"
        mock_config.return_value.api_key = "test-key"
        mock_config.return_value.base_url = ""
        mock_config.return_value.sources = []
        mock_stream.return_value = iter([("delta", "ok")])
        mock_complete.return_value = "\n".join(
            [
                "RAG_REQUIRED",
                "QUERY: 深海図書館 アビス リブラ 本 保存",
                "REASON: named local setting",
            ]
        )
        with TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            (root / "text_10.txt").write_text(
                "深海図書館アビス・リブラでは、本は棚に並ばず、読者の肺活量に合わせて泳いでくる。",
                encoding="utf-8",
            )
            ProjectAccessPath.objects.create(project=self.project, path=str(root), mode="read")

            create = self.client.post(reverse("send_message", args=[self.thread.id]), {"message": "深海図書館アビス・リブラでは本はどのように保存されていますか？"})
            payload = create.json()
            stream = self.client.get(payload["stream_url"])
            b"".join(stream.streaming_content)

        input_text = mock_stream.call_args.args[1]
        self.assertIn("RAG search query: 深海図書館 アビス リブラ 本 保存", input_text)
        self.assertIn("RAG context from allowed local files", input_text)
        self.assertIn("読者の肺活量に合わせて泳いでくる", input_text)
        mock_complete.assert_called_once()

    @patch("agent.views.complete_response")
    @patch("agent.views.stream_response")
    @patch("agent.views.load_runtime_config")
    def test_streaming_returns_no_info_when_search_needed_but_bm25_not_relevant(self, mock_config, mock_stream, mock_complete):
        mock_config.return_value.model = "test-model"
        mock_config.return_value.api_key = "test-key"
        mock_config.return_value.base_url = ""
        mock_config.return_value.sources = []
        mock_stream.return_value = iter([("delta", "should not call")])
        mock_complete.return_value = '{"relevant_indexes": [], "reason": "unrelated"}'
        with TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            (root / "budget.txt").write_text("cost amount", encoding="utf-8")
            ProjectAccessPath.objects.create(project=self.project, path=str(root), mode="read")

            create = self.client.post(reverse("send_message", args=[self.thread.id]), {"message": "astronomyについて要約して"})
            payload = create.json()
            stream = self.client.get(payload["stream_url"])
            body = b"".join(stream.streaming_content).decode()

        assistant = Message.objects.get(id=payload["assistant_id"])
        self.assertIn("十分な情報が見つかりません", assistant.content)
        mock_stream.assert_not_called()
        mock_complete.assert_called_once()

    @patch("agent.views.complete_response")
    @patch("agent.views.stream_response")
    @patch("agent.views.load_runtime_config")
    def test_bm25_inadequate_uses_llm_judge_for_top_k_files(self, mock_config, mock_stream, mock_complete):
        mock_config.return_value.model = "test-model"
        mock_config.return_value.api_key = "test-key"
        mock_config.return_value.base_url = ""
        mock_config.return_value.sources = []
        mock_stream.return_value = iter([("delta", "ok")])
        mock_complete.return_value = '{"relevant_indexes": [1], "reason": "contains relevant lore"}'
        AppSetting.objects.create(key="rag_top_k", value="1")
        with TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            (root / "a_notes.txt").write_text("books are sealed in glass", encoding="utf-8")
            (root / "budget.txt").write_text("cost amount", encoding="utf-8")
            ProjectAccessPath.objects.create(project=self.project, path=str(root), mode="read")

            create = self.client.post(reverse("send_message", args=[self.thread.id]), {"message": "深海図書館の本について要約して"})
            payload = create.json()
            stream = self.client.get(payload["stream_url"])
            b"".join(stream.streaming_content)

        input_text = mock_stream.call_args.args[1]
        self.assertIn("RAG context from allowed local files", input_text)
        self.assertIn("a_notes.txt", input_text)
        self.assertIn("sealed in glass", input_text)
        self.assertNotIn("budget.txt", input_text)
        mock_complete.assert_called_once()

    @patch("agent.views.stream_response")
    @patch("agent.views.load_runtime_config")
    def test_streaming_auto_attaches_allowed_directory_listing_for_list_request(self, mock_config, mock_stream):
        mock_config.return_value.model = "test-model"
        mock_config.return_value.api_key = "test-key"
        mock_config.return_value.base_url = ""
        mock_config.return_value.sources = []
        mock_stream.return_value = iter([("delta", "ok")])
        with TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            (root / "alpha.txt").write_text("a", encoding="utf-8")
            (root / "nested").mkdir()
            ProjectAccessPath.objects.create(project=self.project, path=str(root), mode="read")

            create = self.client.post(reverse("send_message", args=[self.thread.id]), {"message": "ファイルの一覧をください"})
            payload = create.json()
            stream = self.client.get(payload["stream_url"])
            b"".join(stream.streaming_content)

        input_text = mock_stream.call_args.args[1]
        self.assertIn("RAG context from allowed local files", input_text)
        self.assertIn("[file] alpha.txt", input_text)
        self.assertIn("[dir] nested", input_text)

    @patch("agent.views.run_sandbox")
    @patch("agent.views.load_runtime_config")
    def test_plan_runs_rag_then_sandbox_when_both_are_needed(self, mock_config, mock_run_sandbox):
        class Config:
            model = "test-model"
            api_key = "test-key"
            base_url = ""
            sources = []
            sandbox_allowed_libraries = []
            sandbox_image = "python:3.11-slim"
            sandbox_timeout_seconds = 20

            def tool_enabled(self, name, default=False):
                return name in {"rag", "sandbox"}

        mock_config.return_value = Config()
        mock_run_sandbox.return_value = SandboxResult(True, "2")
        with TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            (root / "numbers.txt").write_text("1\n1\n", encoding="utf-8")
            ProjectAccessPath.objects.create(project=self.project, path=str(root), mode="read")

            create = self.client.post(reverse("send_message", args=[self.thread.id]), {"message": "numbersファイルの合計を正確に計算して"})
            payload = create.json()
            stream = self.client.get(payload["stream_url"])
            b"".join(stream.streaming_content)

        sandbox_input = mock_run_sandbox.call_args.args[0]
        self.assertIn("RAG context from allowed local files", sandbox_input)
        assistant = Message.objects.get(id=payload["assistant_id"])
        self.assertIn("Sandbox実行結果: 成功", assistant.content)

    @patch("agent.views.stream_response")
    @patch("agent.views.run_sandbox")
    @patch("agent.views.load_runtime_config")
    def test_named_csv_runs_rag_then_sandbox_without_llm_code_response(self, mock_config, mock_run_sandbox, mock_stream):
        class Config:
            model = "test-model"
            api_key = "test-key"
            base_url = ""
            sources = []
            sandbox_allowed_libraries = []
            sandbox_image = "python:3.11-slim"
            sandbox_timeout_seconds = 20

            def tool_enabled(self, name, default=False):
                return name in {"rag", "sandbox"}

        mock_config.return_value = Config()
        mock_run_sandbox.return_value = SandboxResult(True, "column: テストの点\nsum: 246")
        mock_stream.return_value = iter([("delta", "should not call")])
        with TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            (root / "test.csv").write_text(
                "名前,テストの種類,テストの点\n佐藤太郎,国語,82\n佐藤太郎,数学,76\n佐藤太郎,英語,88\n",
                encoding="utf-8",
            )
            ProjectAccessPath.objects.create(project=self.project, path=str(root), mode="read")

            create = self.client.post(
                reverse("send_message", args=[self.thread.id]),
                {"message": "test.csvの全てのテストの点を足し合わせてください。"},
            )
            payload = create.json()
            stream = self.client.get(payload["stream_url"])
            b"".join(stream.streaming_content)

        sandbox_input = mock_run_sandbox.call_args.args[0]
        self.assertIn("RAG context from allowed local files", sandbox_input)
        self.assertIn("test.csv", sandbox_input)
        assistant = Message.objects.get(id=payload["assistant_id"])
        self.assertIn("Sandbox実行結果: 成功", assistant.content)
        self.assertIn("sum: 246", assistant.content)
        mock_stream.assert_not_called()

    @patch("agent.views.stream_response")
    @patch("agent.views.run_sandbox")
    @patch("agent.views.generate_sandbox_code")
    @patch("agent.views.load_runtime_config")
    def test_grouped_named_csv_generates_code_then_runs_sandbox(
        self,
        mock_config,
        mock_generate_sandbox_code,
        mock_run_sandbox,
        mock_stream,
    ):
        class Config:
            model = "test-model"
            api_key = "test-key"
            base_url = ""
            sources = []
            sandbox_allowed_libraries = []
            sandbox_image = "python:3.11-slim"
            sandbox_timeout_seconds = 20

            def tool_enabled(self, name, default=False):
                return name in {"rag", "sandbox"}

        mock_config.return_value = Config()
        mock_generate_sandbox_code.return_value = "print('佐藤太郎: 246')"
        mock_run_sandbox.return_value = SandboxResult(True, "佐藤太郎: 246")
        mock_stream.return_value = iter([("delta", "should not call")])
        with TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            (root / "test.csv").write_text(
                "名前,テストの種類,テストの点\n佐藤太郎,国語,82\n佐藤太郎,数学,76\n佐藤太郎,英語,88\n",
                encoding="utf-8",
            )
            ProjectAccessPath.objects.create(project=self.project, path=str(root), mode="read")

            create = self.client.post(
                reverse("send_message", args=[self.thread.id]),
                {"message": "test.csvのテストの点を名前ごとに全ての科目を合計してください。"},
            )
            payload = create.json()
            stream = self.client.get(payload["stream_url"])
            b"".join(stream.streaming_content)

        mock_generate_sandbox_code.assert_called_once()
        sandbox_input = mock_run_sandbox.call_args.args[0]
        self.assertIn("Generated sandbox program", sandbox_input)
        self.assertIn("print('佐藤太郎: 246')", sandbox_input)
        assistant = Message.objects.get(id=payload["assistant_id"])
        self.assertIn("Sandbox実行結果: 成功", assistant.content)
        self.assertIn("佐藤太郎: 246", assistant.content)
        mock_stream.assert_not_called()

    def test_approval_actions_never_execute_command(self):
        approval = ApprovalRequest.objects.create(thread=self.thread, command="rm -rf /tmp/example")

        response = self.client.post(reverse("approval_action", args=[approval.id]), {"action": "approve"})

        self.assertEqual(response.status_code, 302)
        approval.refresh_from_db()
        self.assertEqual(approval.status, "approved")

    def test_add_access_path_normalizes_and_saves_mode(self):
        with TemporaryDirectory() as root_dir:
            response = self.client.post(
                reverse("add_access_path", args=[self.project.id]),
                {"path": root_dir, "mode": "write", "note": "workspace"},
            )

        self.assertEqual(response.status_code, 302)
        access = ProjectAccessPath.objects.get(project=self.project)
        self.assertEqual(access.mode, "write")
        self.assertEqual(access.note, "workspace")
        self.assertEqual(access.path, normalize_access_path(root_dir))

    def test_delete_access_path_removes_entry(self):
        access = ProjectAccessPath.objects.create(project=self.project, path="/tmp/example", mode="read")

        response = self.client.post(reverse("delete_access_path", args=[access.id]))

        self.assertEqual(response.status_code, 302)
        self.assertFalse(ProjectAccessPath.objects.filter(id=access.id).exists())

    def test_access_helper_distinguishes_read_and_write(self):
        with TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            child = root / "child.txt"
            child.write_text("x")
            ProjectAccessPath.objects.create(project=self.project, path=str(root), mode="read")

            self.assertTrue(is_path_allowed(self.project, str(child), write=False))
            self.assertFalse(is_path_allowed(self.project, str(child), write=True))

            ProjectAccessPath.objects.create(project=self.project, path=str(root), mode="write")
            self.assertTrue(is_path_allowed(self.project, str(child), write=True))


@override_settings(STATICFILES_DIRS=[])
class DirectoryBrowseTests(TestCase):
    def test_browse_directories_lists_visible_child_folders(self):
        with TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            repo = root / "repo"
            repo.mkdir()
            (repo / ".git").mkdir()
            (root / ".hidden").mkdir()
            (root / "notes.txt").write_text("not a directory")

            response = self.client.get(reverse("browse_directories"), {"path": str(root)})

            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["current"], str(root.resolve()))
            self.assertEqual(len(payload["directories"]), 1)
            self.assertEqual(payload["directories"][0]["name"], "repo")
            self.assertTrue(payload["directories"][0]["is_repo"])

    def test_browse_directories_rejects_missing_path(self):
        response = self.client.get(reverse("browse_directories"), {"path": "/definitely/missing/path"})

        self.assertEqual(response.status_code, 404)


class OpenAIClientTests(TestCase):
    def test_auto_mode_falls_back_to_chat_completions(self):
        class Config:
            model = "model"
            api_key = "key"
            base_url = ""
            api_mode = "auto"

        class Responses:
            def stream(self, **kwargs):
                raise RuntimeError("responses unsupported")

        class ChatCompletions:
            def create(self, **kwargs):
                delta = type("Delta", (), {"content": "ok"})()
                choice = type("Choice", (), {"delta": delta})()
                yield type("Chunk", (), {"id": "chat_1", "choices": [choice]})()

        class Chat:
            completions = ChatCompletions()

        class Client:
            responses = Responses()
            chat = Chat()

        with patch("agent.openai_client.OpenAI", return_value=Client()):
            events = list(stream_response(Config(), "hello", "system"))

        self.assertEqual(events, [("delta", "ok"), ("response_id", "chat_1")])

    def test_ollama_uses_openai_compatible_client_with_default_base_url_and_dummy_key(self):
        config = RuntimeConfig(
            values={
                "providers": {
                    "ollama": {
                        "enabled": True,
                        "model": "llama3.1",
                    }
                }
            },
            sources=[],
        )

        class ChatCompletions:
            def create(self, **kwargs):
                self.kwargs = kwargs
                message = type("Message", (), {"content": "local ok"})()
                choice = type("Choice", (), {"message": message})()
                return type("Response", (), {"choices": [choice]})()

        class Chat:
            completions = ChatCompletions()

        class Client:
            chat = Chat()

        with patch("agent.openai_client.OpenAI", return_value=Client()) as mock_openai:
            from .openai_client import complete_response

            result = complete_response(config, "hello")

        self.assertEqual(result, "local ok")
        mock_openai.assert_called_once_with(api_key="ollama", base_url="http://localhost:11434/v1")

    def test_azure_provider_uses_azure_client(self):
        config = RuntimeConfig(
            values={
                "providers": {
                    "azure": {
                        "enabled": True,
                        "model": "deployment",
                        "api_key": "azure-key",
                        "azure_endpoint": "https://example.openai.azure.com",
                        "api_version": "2024-02-15-preview",
                    }
                }
            },
            sources=[],
        )

        class ChatCompletions:
            def create(self, **kwargs):
                message = type("Message", (), {"content": "azure ok"})()
                choice = type("Choice", (), {"message": message})()
                return type("Response", (), {"choices": [choice]})()

        class Chat:
            completions = ChatCompletions()

        class Client:
            chat = Chat()

        with patch("agent.openai_client.AzureOpenAI", return_value=Client()) as mock_azure:
            from .openai_client import complete_response

            result = complete_response(config, "hello")

        self.assertEqual(result, "azure ok")
        mock_azure.assert_called_once_with(
            api_key="azure-key",
            azure_endpoint="https://example.openai.azure.com",
            api_version="2024-02-15-preview",
        )

    def test_bedrock_provider_uses_converse(self):
        config = RuntimeConfig(
            values={
                "providers": {
                    "bedrock": {
                        "enabled": True,
                        "model": "anthropic.test-model",
                        "region": "ap-northeast-1",
                    }
                }
            },
            sources=[],
        )

        class BedrockClient:
            def converse(self, **kwargs):
                self.kwargs = kwargs
                return {"output": {"message": {"content": [{"text": "bedrock ok"}]}}}

        with patch("agent.openai_client._bedrock_client", return_value=BedrockClient()):
            from .openai_client import complete_response

            result = complete_response(config, "hello", "system")

        self.assertEqual(result, "bedrock ok")
