import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from processwatch.config import Config


class ConfigTests(unittest.TestCase):
    def test_loads_dotenv_and_preserves_environment_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, ".env").write_text(
                "# local settings\nDATAJUD_API_KEY=from-file\nPROCESSWATCH_INTERVAL='3600'\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"DATAJUD_API_KEY": "from-environment"}, clear=True):
                with patch("pathlib.Path.cwd", return_value=Path(directory)):
                    config = Config.from_env()
            self.assertEqual(config.api_key, "from-environment")
            self.assertEqual(config.interval, 3600)


if __name__ == "__main__":
    unittest.main()

