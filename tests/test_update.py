"""Unit tests offline: nenhum acesso a GitHub/GitLab."""
import json
import shutil
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import update


class FakeAPI:
    github_token = "test-token"
    def __init__(self, failure=False):
        self.failure = failure
    def get(self, url, *, missing_ok=False):
        if self.failure:
            raise update.FetchFailure("simulated-outage")
        if url.startswith("https://api.github.com/search/code?"):
            return {"items": []}
        if url.endswith("/releases?per_page=8"):
            if "/Alpha/" in url:
                return [{"draft": False, "prerelease": False, "assets": [{"name": "Alpha-Windows-x64.zip", "browser_download_url": "https://github.com/owner/Alpha/releases/download/v1/Alpha-Windows-x64.zip"}]}]
            if "/Gamma/" in url:
                return [{"assets": [{"name": "Gamma-windows.zip", "browser_download_url": "https://github.com/download.zip"}]}]
            return []
        if "/contents/" in url:
            return None
        if url.endswith("/Alpha") or url.endswith("/Beta") or url.endswith("/Gamma"):
            return {"default_branch": "main"}
        return None


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        for p in ("config", "data", "output"):
            (self.base / p).mkdir()
        (self.base / "config/sources.json").write_text(json.dumps({
            "autoDiscoverGitHubManifests": False,
            "repositories": [
                {"host": "github", "repo": "owner/Alpha", "name": "Alpha", "kind": "port", "curatedStatus": "released", "curatedWindows": True},
                {"host": "github", "repo": "owner/Beta", "name": "Beta", "kind": "recomp", "curatedStatus": "in-progress"},
                {"host": "github", "repo": "owner/Gamma", "name": "Gamma", "kind": "decomp", "curatedStatus": "complete", "curatedWindows": True}
            ]}), encoding="utf-8")
        (self.base / "config/overrides.json").write_text(json.dumps({"repositories": {}, "excludedRepositories": []}), encoding="utf-8")
        (self.base / "data/discovered_repos.json").write_text('{"repositories": []}', encoding="utf-8")
        (self.base / "config/minha-lista-114.json").write_text(json.dumps({"apps": [{"name": "Legacy", "repository": "old/legacy", "folderName": "Legacy", "appIconUrl": None}]}), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_classes_and_combines(self):
        report = update.regenerate(FakeAPI(), self.base, discovery=False)
        self.assertEqual(report["playable_windows"], 1)
        self.assertEqual(report["development"], 1)
        self.assertEqual(report["combined_with_legacy"], 3)
        p = update.read_json(self.base / "output/windows-jogaveis.json")
        self.assertEqual(p["apps"][0]["repository"], "owner/Alpha")
        d = update.read_json(self.base / "output/em-desenvolvimento.json")
        self.assertIn("em-desenvolvimento", d["apps"][0]["tags"])
        self.assertNotIn("owner/Gamma", str(update.read_json(self.base / "output/tudo-quiver.json")))

    def test_does_not_overwrite_on_network_failure(self):
        original = '{"apps":[{"name":"keep"}]}'
        f = self.base / "output/windows-jogaveis.json"
        f.write_text(original)
        with self.assertRaises(update.FetchFailure):
            update.regenerate(FakeAPI(failure=True), self.base, discovery=False)
        self.assertEqual(f.read_text(), original)

    def test_classification_requires_windows_file(self):
        d = {"status": "playable", "targets": ["Windows"], "game": "Some Game", "type": "recomp", "system": "N64"}
        release = [{"assets": [{"name": "game-linux-x64.zip", "browser_download_url": "https://github.com/example"}]}]
        entry, category, _ = update.make_entry("github", "owner/Some", {"manifest": d, "releases": release}, {}, {})
        self.assertIsNone(entry)
        self.assertEqual(category, "ignored")
        release[0]["assets"][0]["name"] = "game-win64.zip"
        entry, category, _ = update.make_entry("github", "owner/Some", {"manifest": d, "releases": release}, {}, {})
        self.assertEqual(category, "playable")

    def test_decomp_only_not_installable(self):
        d = {"status": "complete", "targets": ["Windows"], "game": "Some Game", "type": "decomp", "system": "N64"}
        release = [{"assets": [{"name": "game-win64.zip", "browser_download_url": "https://github.com/example"}]}]
        _, category, _ = update.make_entry("github", "owner/Some", {"manifest": d, "releases": release}, {}, {})
        self.assertEqual(category, "ignored")

    def test_manifest_has_precedence_over_curated_status(self):
        m = {"game": "Alpha", "system": "N64", "type": "recomp", "status": "paused", "targets": ["Windows"]}
        seed = {"name": "Alpha", "kind": "recomp", "curatedStatus": "released", "curatedWindows": True}
        releases = [{"assets": [{"name": "Alpha-windows.zip", "browser_download_url": "https://github.com/a/b"}]}]
        entry, category, _ = update.make_entry("github", "owner/Alpha", {"manifest": m, "seed": seed, "releases": releases}, {}, {})
        self.assertEqual(category, "development")
        self.assertIn("paused", entry["tags"])

    def test_incremental_batches_keep_previous_entries(self):
        cfg = update.read_json(self.base / "config/sources.json")
        cfg["maxRepositoriesPerRun"] = 2
        update.write_json_if_changed(self.base / "config/sources.json", cfg)
        one = update.regenerate(FakeAPI(), self.base, discovery=False)
        self.assertEqual(one["checked_this_run"], 2)
        self.assertEqual(one["awaiting_first_check"], 1)
        self.assertEqual(one["combined_with_legacy"], 3)
        two = update.regenerate(FakeAPI(), self.base, discovery=False)
        self.assertEqual(two["checked_this_run"], 2)
        self.assertEqual(two["awaiting_first_check"], 0)
        self.assertEqual(two["combined_with_legacy"], 3)

    def test_gitlab_author_manifest_uses_repository_source(self):
        m = {"game": "Example", "system": "N64", "type": ["decomp", "port"],
             "status": "playable", "targets": ["Windows"]}
        releases = [{"assets": {"links": [{"name": "Example-Windows.zip", "url": "https://gitlab.com/example/-/releases/download.zip"}]}}]
        entry, category, _ = update.make_entry("gitlab", "group/subproject", {"manifest": m, "releases": releases}, {}, {})
        self.assertEqual(category, "playable")
        self.assertEqual(entry["repositorySource"], "gitlab")

    def test_rejects_personal_profile_icons_by_default(self):
        d = {"status": "in-progress", "game": "Some Game", "type": "port", "system": "N64"}
        e, c, _ = update.make_entry("github", "owner/Some", {"manifest": d, "releases": []}, {}, {})
        self.assertIsNone(e["appIconUrl"])


if __name__ == "__main__":
    unittest.main()
