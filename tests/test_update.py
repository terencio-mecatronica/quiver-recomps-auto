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


    def test_legacy_steam_art_is_kept_and_personal_avatar_is_removed(self):
        source = [
            {"repository": "example/Game", "appIconUrl": "https://cdn.cloudflare.steamstatic.com/steam/apps/100/header.jpg"},
            {"repository": "author/Project", "appIconUrl": "https://github.com/author.png?size=512"},
            {"repository": "other/Project", "appIconUrl": "https://avatars.githubusercontent.com/u/123456"},
        ]
        result = update.with_curated_images(source, {"images": {}, "repositories": {}}, legacy=True)
        self.assertEqual(result[0]["appIconUrl"], source[0]["appIconUrl"])
        self.assertIsNone(result[1]["appIconUrl"])
        self.assertIsNone(result[2]["appIconUrl"])
        self.assertEqual(source[1]["appIconUrl"], "https://github.com/author.png?size=512")

    def test_curated_logo_applies_even_without_old_image(self):
        src = [{"repository": "org/Game", "folderName": "Game", "appIconUrl": None}]
        settings = {"images": {"github:org/Game": "https://example.org/game-logo.png"}}
        self.assertEqual(update.with_curated_images(src, settings)[0]["appIconUrl"],
                         "https://example.org/game-logo.png")

    def test_manual_null_removes_even_approved_legacy_art(self):
        source = [{"repository": "org/Game", "appIconUrl": "https://cdn.cloudflare.steamstatic.com/steam/apps/100/header.jpg"}]
        settings = {"images": {"github:org/Game": None}, "repositories": {}}
        self.assertIsNone(update.with_curated_images(source, settings, legacy=True)[0]["appIconUrl"])

    def test_image_map_beats_old_repository_override(self):
        settings = {"images": {"github:org/Game": "https://example.com/new.png"},
                    "repositories": {"github:org/Game": {"appIconUrl": "https://example.com/old.png"}}}
        self.assertEqual(update.curated_image("github", "org/Game", None, settings), "https://example.com/new.png")

    def test_gitlab_repo_can_have_image_override(self):
        settings = {"images": {"gitlab:group/repo": "https://gitlab.com/group/repo/-/raw/main/logo.png"}}
        self.assertEqual(update.curated_image("gitlab", "group/repo", None, settings),
                         "https://gitlab.com/group/repo/-/raw/main/logo.png")

    def test_bad_image_url_fails_fast(self):
        for url in ("http://example.com/logo.png", "file:///tmp/private.txt", "javascript:alert(1)"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                update.curated_image("github", "org/Game", None,
                                     {"images": {"github:org/Game": url}})

    def test_cached_project_receives_override_without_rechecking(self):
        cfg = update.read_json(self.base / "config/sources.json")
        cfg["maxRepositoriesPerRun"] = 1
        update.write_json_if_changed(self.base / "config/sources.json", cfg)
        # Rodada 1: consulta Alpha; rodada 2: Beta fica no cache como desenvolvimento
        update.regenerate(FakeAPI(), self.base, discovery=False)
        update.regenerate(FakeAPI(), self.base, discovery=False)
        options = update.read_json(self.base / "config/overrides.json")
        options["images"] = {"github:owner/Beta": "https://example.org/beta-official.png"}
        update.write_json_if_changed(self.base / "config/overrides.json", options)
        # Rodada 3 consulta Gamma, sem visitar Beta novamente.
        update.regenerate(FakeAPI(), self.base, discovery=False)
        items = update.read_json(self.base / "output/em-desenvolvimento.json")["apps"]
        self.assertEqual(items[0]["appIconUrl"], "https://example.org/beta-official.png")
        # Limpar override volta a null sem novo ciclo de verificacao da API.
        options["images"] = {}
        update.write_json_if_changed(self.base / "config/overrides.json", options)
        update.regenerate(FakeAPI(), self.base, discovery=False)
        self.assertIsNone(update.read_json(self.base / "output/em-desenvolvimento.json")["apps"][0]["appIconUrl"])


if __name__ == "__main__":
    unittest.main()

class BroadDiscoveryTests(unittest.TestCase):
    """Cobertura da v3 sem rede: reproduz os dados essenciais de LASRecomp."""
    class BroadFake:
        github_token = "fake-token"
        def __init__(self, windows=True, wip=False, description="Static recompilation of Link's Awakening (Switch) to native macOS, Windows and Linux"):
            self.windows = windows
            self.wip = wip
            self.description = description
            self.calls = []
        def get(self, url, *, missing_ok=False):
            import base64
            self.calls.append(url)
            if "/search/repositories?" in url:
                return {"items": [{"full_name": "peiikai/LASRecomp", "private": False,
                                   "fork": False, "archived": False, "description": self.description}],
                        "incomplete_results": False}
            if "/search/code?" in url:
                return {"items": []}
            if url.endswith("/repos/peiikai/LASRecomp"):
                return {"name": "LASRecomp", "default_branch": "main", "description": self.description}
            if "/contents/" in url:
                return None
            if url.endswith("/repos/peiikai/LASRecomp/readme"):
                readme = "# LASRecomp\nStatic recompilation of Link's Awakening (Switch) to Windows."
                if self.wip:
                    readme += "\nThis is a work in progress."
                return {"encoding": "base64", "content": base64.b64encode(readme.encode()).decode()}
            if url.endswith("/repos/peiikai/LASRecomp/releases?per_page=8"):
                if self.windows:
                    return [{"draft": False, "prerelease": True,
                             "assets": [{"name": "LAS.Recomp-beta_4-windows.zip",
                                         "browser_download_url": "https://github.com/peiikai/LASRecomp/releases/download/beta4/LAS.Recomp-beta_4-windows.zip"}]}]
                return []
            return None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        for folder in ("config", "data", "output"):
            (self.base / folder).mkdir()
        (self.base / "config/sources.json").write_text(json.dumps({
            "autoDiscoverGitHubManifests": False,
            "autoDiscoverGitHubRepositories": True,
            "repositorySearchQueries": ['"static recompilation" in:description'],
            "repositorySearchPages": 1, "maxRepositoriesPerRun": 10,
            "repositories": []}), encoding="utf8")
        (self.base / "config/overrides.json").write_text('{"repositories": {}, "images": {}}', encoding="utf8")
        (self.base / "config/minha-lista-114.json").write_text('{"apps": []}', encoding="utf8")
        (self.base / "data/discovered_repos.json").write_text('{"repositories": []}', encoding="utf8")
    def tearDown(self): self.tmp.cleanup()

    def test_las_discovered_without_seed_or_manifest(self):
        api = self.BroadFake()
        summary = update.regenerate(api, self.base)
        self.assertEqual(summary["playable_windows"], 1)
        self.assertEqual(summary["combined_with_legacy"], 1)
        row = update.read_json(self.base / "output/windows-jogaveis.json")["apps"][0]
        self.assertEqual(row["repository"], "peiikai/LASRecomp")
        self.assertIn("pode-ser-beta", row["tags"])
        report = update.read_json(self.base / "output/relatorio.json")
        self.assertEqual(report["projects"][0]["classification"], "playable")
        self.assertIn('"static recompilation" in:description', report["projects"][0]["matched_queries"])
        self.assertTrue(any("/readme" in x for x in api.calls))

    def test_explicit_wip_goes_to_development_without_build(self):
        api = self.BroadFake(windows=False, wip=True)
        result = update.regenerate(api, self.base)
        self.assertEqual(result["development"], 1)
        self.assertEqual(result["playable_windows"], 0)

    def test_unclear_recomp_goes_to_review_not_app_list(self):
        api = self.BroadFake(windows=False, wip=False)
        result = update.regenerate(api, self.base)
        self.assertEqual(result["review_candidates"], 1)
        self.assertEqual(result["combined_with_legacy"], 0)
        reviewed = update.read_json(self.base / "output/candidatos-revisao.json")["review"]
        self.assertEqual(reviewed[0]["repo"], "peiikai/LASRecomp")

    def test_rejects_unrelated_repo_even_if_search_finds_it(self):
        api = self.BroadFake(description="Framework and SDK for static recompilation of games")
        result = update.regenerate(api, self.base)
        self.assertEqual(result["playable_windows"], 0)
        self.assertEqual(result["review_candidates"], 1)

    def test_search_skips_private_archived_fork_and_bad_names(self):
        class SearchOnly:
            def get(self, url):
                return {"items": [{"full_name": "u/One", "private": True},
                                  {"full_name": "u/Two", "archived": True},
                                  {"full_name": "u/Three", "fork": True},
                                  {"full_name": "u/Four", "private": False},
                                  {"full_name": "u/Four", "private": False},
                                  {"full_name": "bad"}]}
        found, stats = update.discover_repository_search(SearchOnly(), ["recomp in:name"])
        self.assertEqual([r["repo"] for r in found], ["u/Four"])
        self.assertEqual(stats["requests"], 1)

    def test_curated_asset_detection_allows_beta_not_draft(self):
        release = [{"draft": False, "prerelease": True, "assets": [
            {"name": "LAS.Recomp-beta_4-windows.zip", "browser_download_url": "https://github.com/example"}]}]
        self.assertTrue(update.has_windows_asset(release, "github")[0])
        release[0]["draft"] = True
        self.assertFalse(update.has_windows_asset(release, "github")[0])

    def test_discovery_unavailable_preserves_cache(self):
        api = self.BroadFake()
        update.regenerate(api, self.base)
        class FailedSearch(self.BroadFake):
            def get(self, url, *, missing_ok=False):
                if "/search/repositories?" in url:
                    raise update.FetchFailure("rate limited")
                return super().get(url, missing_ok=missing_ok)
        result = update.regenerate(FailedSearch(), self.base)
        self.assertEqual(result["playable_windows"], 1)
        self.assertIn("error", result["search"])
