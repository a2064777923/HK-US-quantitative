import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class FeishuNotifyTests(unittest.TestCase):
    def load_module(self, **env):
        keys = {"FEISHU_ENV_FILE", "FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_CHAT_ID"}
        old = {key: os.environ.get(key) for key in keys}
        try:
            for key in keys:
                os.environ.pop(key, None)
            os.environ.update(env)
            import scripts.feishu_notify as feishu

            module = importlib.reload(feishu)
            module._token_cache = {"token": None, "expires": 0}
            return module
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_config_reads_env_file_defaults_without_code_secrets(self):
        with tempfile.TemporaryDirectory() as td:
            env_file = Path(td) / "quantmind.env"
            env_file.write_text(
                "\n".join(
                    [
                        'export FEISHU_APP_ID="app-id"',
                        "FEISHU_APP_SECRET='app-secret'",
                        "FEISHU_CHAT_ID=chat-id",
                    ]
                ),
                encoding="utf-8",
            )
            feishu = self.load_module(FEISHU_ENV_FILE=str(env_file))

            self.assertEqual(
                feishu.feishu_config(),
                {
                    "FEISHU_APP_ID": "app-id",
                    "FEISHU_APP_SECRET": "app-secret",
                    "FEISHU_CHAT_ID": "chat-id",
                },
            )

    def test_missing_credentials_do_not_call_token_endpoint(self):
        feishu = self.load_module(FEISHU_ENV_FILE="/tmp/does-not-exist")

        with patch.object(feishu.urllib.request, "urlopen") as urlopen:
            self.assertIsNone(feishu.get_tenant_token())

        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
