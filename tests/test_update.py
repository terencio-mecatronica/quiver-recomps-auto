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


class Version31Tests(unittest.TestCase):
    """Regressao da v3.1: cobertura paginada, fila e classificacao conservadora."""

    def test_searches_two_pages_of_100_and_sorts_by_update(self):
        from urllib.parse import parse_qs, urlparse
        class SearchAPI:
            def __init__(self): self.calls = []
            def get(self, url):
                q = parse_qs(urlparse(url).query)
                self.calls.append(q)
                page = int(q["page"][0])
                if page == 1:
                    return {"items": [{"full_name": f"owner/Game{i}", "pushed_at": "2026-10-15T00:00:00Z"}
                                      for i in range(100)]}
                return {"items": [{"full_name": "owner/Page2"}]}
        api = SearchAPI()
        found, stats = update.discover_repository_search(
            api, ['"static recompilation" in:description'])
        self.assertEqual(stats["requests"], 2)
        self.assertEqual(len(found), 101)
        self.assertEqual([int(c["page"][0]) for c in api.calls], [1, 2])
        self.assertEqual(api.calls[0]["per_page"], ["100"])
        self.assertEqual(api.calls[0]["sort"], ["updated"])
        self.assertEqual(api.calls[0]["order"], ["desc"])

    def test_rotates_extra_pages_without_losing_first_page(self):
        from urllib.parse import parse_qs, urlparse
        class SearchAPI:
            def __init__(self): self.pages = []
            def get(self, url):
                page = int(parse_qs(urlparse(url).query)["page"][0])
                self.pages.append(page)
                return {"items": [{"full_name": f"org/Repo{page}_{i}"} for i in range(100)]}
        api = SearchAPI()
        update.discover_repository_search(api, ["recomp in:name"], page_window=2, sort=None)
        self.assertEqual(api.pages, [1, 4])

    def test_partial_rate_limit_keeps_successful_search_results(self):
        class SearchAPI:
            calls = 0
            def get(self, url):
                self.calls += 1
                if self.calls == 2:
                    raise update.FetchFailure("HTTP 429")
                return {"items": [{"full_name": f"org/Game{i}"} for i in range(100)]}
        found, stats = update.discover_repository_search(SearchAPI(), ["recomp in:name"])
        self.assertEqual(len(found), 100)
        self.assertEqual(stats["requests"], 1)
        self.assertIn("429", stats["error"])
        self.assertIn("recomp in:name", stats["incomplete_queries"])

    def test_pc_package_requires_explicit_windows_context(self):
        release = [{"draft": False, "assets": [
            {"name": "LASRecomp-beta-PC.zip", "browser_download_url": "https://github.com/example"}]}]
        self.assertFalse(update.has_windows_asset(release, "github")[0])
        self.assertTrue(update.has_windows_asset(release, "github", pc_is_windows=True)[0])
        release[0]["assets"][0]["name"] = "LASRecomp-linux-PC.zip"
        self.assertFalse(update.has_windows_asset(release, "github", pc_is_windows=True)[0])
        release[0]["assets"][0]["name"] = "LASRecomp-PC-source.zip"
        self.assertFalse(update.has_windows_asset(release, "github", pc_is_windows=True)[0])

    def test_early_author_statement_can_confirm_development(self):
        meta = {"name": "SomeRecomp", "description": "Static recompilation of an old game."}
        entry, stage, audit = update.classify_unmanifested(
            "org/Game", meta, "This is an early development project. Work in progress.", [])
        self.assertEqual(stage, "development")
        self.assertIn("windows-nao-confirmado", entry["tags"])
        self.assertFalse(audit["windows_asset_detected"])

    def test_windows_pc_release_requires_author_windows_statement(self):
        release = [{"assets": [{"name": "Game-PC.zip", "browser_download_url": "https://github.com/example"}]}]
        meta = {"name": "Game", "description": "Static recompilation of a game for Windows."}
        entry, stage, _ = update.classify_unmanifested("org/Game", meta, "", release)
        self.assertEqual(stage, "playable")
        self.assertIn("windows", entry["tags"])
        meta["description"] = "Static recompilation of a game for desktop."
        entry, stage, _ = update.classify_unmanifested("org/Game", meta, "", release)
        self.assertEqual(stage, "review")
        self.assertIsNone(entry)

    def test_priority_queue_preserves_already_checked_entries(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            for p in ("data", "config", "output"): (base / p).mkdir()
            def write(rel, content): (base / rel).write_text(json.dumps(content), encoding="utf-8")
            write("config/sources.json", {
                "repositories": [{"host":"github", "repo":f"user/R{i}",
                                  "name":f"R{i}", "kind":"recomp", "curatedStatus":"in-progress"}
                                 for i in range(6)],
                "autoDiscoverGitHubManifests": False, "maxRepositoriesPerRun": 3})
            write("config/overrides.json", {"repositories": {}})
            write("config/minha-lista-114.json", {"apps": []})
            write("data/discovered_repos.json", {"repositories": []})
            write("data/snapshot.json", {"cursor": 0, "repositories": {
                "github:user/R0": {"classification":"development", "entry": {
                    "name":"R0", "project":"R0", "repository":"user/R0", "folderName":"R0",
                    "tags":["recomp-tracker", "recomp", "em-desenvolvimento", "in-progress"],
                    "appIconUrl": None}, "audit":{"repo":"user/R0", "classification":"development"}},
                "github:user/R1": {"classification":"development", "entry": {
                    "name":"R1", "project":"R1", "repository":"user/R1", "folderName":"R1",
                    "tags":["recomp-tracker", "recomp", "em-desenvolvimento", "in-progress"],
                    "appIconUrl": None}, "audit":{"repo":"user/R1", "classification":"development"}}
            }})
            class API:
                github_token = ""
                def __init__(self): self.checked=[]
                def get(self, url, *, missing_ok=False):
                    if url.endswith("/releases?per_page=8"):return []
                    if "/contents/" in url:return None
                    if "/repos/" in url:
                        self.checked.append(url.split("/repos/")[1])
                        return {"name": url.rsplit("/",1)[-1],"default_branch":"main"}
                    return None
            api = API()
            result = update.regenerate(api, base, discovery=False)
            self.assertEqual(result["checked_this_run"], 3)
            self.assertGreaterEqual(len([x for x in api.checked if x not in ("user/R0", "user/R1")]), 2)
            self.assertEqual(result["awaiting_first_check"], 2)
            self.assertIn("user/R0", [a["repository"] for a in
                          update.read_json(base / "output/tudo-quiver.json")["apps"]])

    def test_search_sort_alternates_between_runs(self):
        import tempfile
        from urllib.parse import parse_qs, urlparse
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            for p in ("data", "config", "output"): (base/p).mkdir()
            (base/"config/sources.json").write_text(json.dumps({
                "repositories":[],"autoDiscoverGitHubManifests":False,
                "repositorySearchQueries":["recomp in:name"],"repositorySearchPages":1}))
            (base/"config/overrides.json").write_text('{"repositories":{}}')
            (base/"config/minha-lista-114.json").write_text('{"apps":[]}')
            (base/"data/discovered_repos.json").write_text('{"repositories":[]}')
            class API:
                github_token="token"
                def __init__(self): self.search_params=[]
                def get(self, url, *, missing_ok=False):
                    if "/search/repositories?" in url:
                        self.search_params.append(parse_qs(urlparse(url).query))
                        return {"items":[]}
                    return None
            api = API()
            update.regenerate(api, base)
            update.regenerate(api, base)
            self.assertEqual(api.search_params[0]["sort"],["updated"])
            self.assertNotIn("sort",api.search_params[1])
            self.assertEqual(update.read_json(base/"data/snapshot.json")["search_cycle"],2)



class Version32ArtworkTests(unittest.TestCase):
    """v3.2: imagens do autor, cache, limites e integracao do JSON."""

    def test_artwork_selects_author_cover_over_logo_and_avatar(self):
        tree = {"tree": [
            {"type": "blob", "path": "assets/logo.png", "size": 9000},
            {"type": "blob", "path": "docs/images/cover.webp", "size": 250000},
            {"type": "blob", "path": "vendor/cover.png", "size": 250000},
            {"type": "blob", "path": "assets/avatar.png", "size": 14000},
            {"type": "blob", "path": "docs/test/cover.jpg", "size": 12000},
            {"type": "blob", "path": "assets/banner.svg", "size": 3000},
        ]}
        self.assertEqual(update.choose_author_art(tree), "docs/images/cover.webp")

    def test_readme_relative_cover_has_priority_and_file_must_exist(self):
        tree={"tree":[
            {"type":"blob", "path":"assets/logo.png", "size":10000},
            {"type":"blob", "path":"images/project-banner.png", "size":30000}]}
        text="## Game\n![Official cover](images/project-banner.png)\n![avatar](https://github.com/person.png)"
        self.assertEqual(update.choose_readme_art(text,tree), "images/project-banner.png")
        self.assertIsNone(update.choose_readme_art("![Cover](images/not-exist.png)",tree))
        self.assertIsNone(update.choose_readme_art("![Cover](../private/cover.png)",tree))

    def test_no_avatar_generic_screenshots_or_unsafe_paths(self):
        tree = {"tree": [
            {"type": "blob", "path": "assets/avatar.png", "size": 8000},
            {"type": "blob", "path": "assets/screenshot.png", "size": 80000},
            {"type": "blob", "path": "../cover.png", "size": 9000},
            {"type": "blob", "path": "tests/cover.png", "size": 9000},
            {"type": "blob", "path": "assets/icon.svg", "size": 9000},
        ]}
        self.assertIsNone(update.choose_author_art(tree))

    def test_author_art_url_uses_repository_and_real_branch(self):
        class API:
            def __init__(self): self.calls=[]
            def get(self, url, *, missing_ok=False):
                self.calls.append(url)
                if "/git/trees/" in url:
                    return {"tree": [{"type": "blob", "path": "art/key-art.png", "size": 40000}]}
                return {"default_branch": "release-v4"}
        api = API()
        self.assertEqual(update.discover_author_art(api, "owner/Game"),
            "https://raw.githubusercontent.com/owner/Game/release-v4/art/key-art.png")
        self.assertEqual(len(api.calls), 3)

    def test_no_false_random_art_when_repository_has_none(self):
        class API:
            def get(self, url, *, missing_ok=False):
                if "/git/trees/" in url:
                    return {"tree": [{"type":"blob", "path":"README.md", "size":3000}]}
                return {"default_branch":"main"}
        self.assertIsNone(update.discover_author_art(API(), "owner/Game"))

    def test_artwork_cache_avoids_repeated_api_and_respects_explicit_null(self):
        from datetime import datetime, timezone
        class API:
            def __init__(self): self.calls=[]
            def get(self, url, *, missing_ok=False):
                self.calls.append(url)
                if "/git/trees/" in url:
                    return {"tree": [{"type":"blob", "path":"cover.png", "size":100000}]}
                return {"default_branch":"main"}
        api=API()
        app={"repository":"owner/Game", "appIconUrl":None}
        cache={}
        t=datetime(2026,10,16,tzinfo=timezone.utc)
        first=update.populate_author_images(api,[app],{},cache,max_scans=1,now=t)
        self.assertEqual(first["scanned"],1)
        self.assertIn("cover.png",app["appIconUrl"])
        app["appIconUrl"]=None
        second=update.populate_author_images(api,[app],{},cache,max_scans=1,now=t)
        self.assertEqual(second["scanned"],0)
        self.assertEqual(len(api.calls),3)
        self.assertIn("cover.png",app["appIconUrl"])
        app["appIconUrl"]=None
        update.populate_author_images(api,[app],{"images":{"github:owner/Game": None}},cache,now=t)
        self.assertIsNone(app["appIconUrl"])
        self.assertEqual(len(api.calls),3)

    def test_rate_limit_pauses_optional_artwork_without_breaking_catalog(self):
        class API:
            def get(self, url, *, missing_ok=False):
                raise update.FetchFailure("HTTP 429 em https://api.github.com/repos/... ")
        cache={}
        entries=[{"repository":"owner/A", "appIconUrl":None},
                 {"repository":"owner/B", "appIconUrl":None}]
        summary=update.populate_author_images(API(),entries,{},cache)
        self.assertTrue(summary["rate_limited"])
        self.assertEqual(summary["scanned"],0)
        self.assertEqual(cache,{})

    def test_end_to_end_art_in_all_lists(self):
        # Importa FakeAPI para manter a fixture original da v3.1.
        with tempfile.TemporaryDirectory() as td:
            base=Path(td)
            for folder in ("data", "config", "output"): (base/folder).mkdir()
            (base/"config/sources.json").write_text(json.dumps({
                "repositories":[{"host":"github", "repo":"owner/Alpha", "name":"Alpha",
                                  "kind":"port", "curatedStatus":"released", "curatedWindows":True}],
                "autoDiscoverGitHubManifests":False,"maxArtworkScansPerRun":3}),encoding="utf-8")
            (base/"config/overrides.json").write_text('{"repositories":{}}')
            (base/"data/discovered_repos.json").write_text('{"repositories":[]}')
            (base/"config/minha-lista-114.json").write_text('{"apps":[]}')
            class ArtAPI(FakeAPI):
                def get(self, url, *, missing_ok=False):
                    if "/git/trees/" in url:
                        return {"tree":[{"type":"blob", "path":"assets/cover.jpg", "size":200000}]}
                    return super().get(url, missing_ok=missing_ok)
            summary=update.regenerate(ArtAPI(),base,discovery=False)
            url="https://raw.githubusercontent.com/owner/Alpha/main/assets/cover.jpg"
            self.assertEqual(summary["with_images"],1)
            self.assertEqual(summary["image_discovery"]["scanned"],1)
            self.assertEqual(update.read_json(base/"output/windows-jogaveis.json")["apps"][0]["appIconUrl"],url)
            self.assertEqual(update.read_json(base/"output/tudo-quiver.json")["apps"][0]["appIconUrl"],url)
            self.assertEqual(update.read_json(base/"data/snapshot.json")["artwork"]["owner/Alpha"]["url"],url)

    def test_code_search_http_429_cached_for_24_hours(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td)
            for folder in ("data", "config", "output"): (base/folder).mkdir()
            (base/"config/sources.json").write_text(json.dumps({
                "repositories":[],"autoDiscoverGitHubRepositories":False,
                "autoDiscoverGitHubManifests":True}))
            (base/"config/overrides.json").write_text('{"repositories":{}}')
            (base/"data/discovered_repos.json").write_text('{"repositories":[]}')
            (base/"config/minha-lista-114.json").write_text('{"apps":[]}')
            class LimitedAPI:
                github_token="test"
                def __init__(self): self.calls=0
                def get(self,url,*,missing_ok=False):
                    if "/search/code?" in url:
                        self.calls+=1
                        raise update.FetchFailure("HTTP 429 em https://api.github.com/search/code")
                    return None
            api=LimitedAPI()
            update.regenerate(api,base)
            update.regenerate(api,base)
            self.assertEqual(api.calls,1)
            self.assertTrue(update.read_json(base/"data/snapshot.json")["manifest_retry_at"])

    def test_v32_workflow_has_six_runs_and_manual_trigger(self):
        workflow=(Path(__file__).resolve().parents[1]/".github/workflows/update.yml")
        self.assertTrue(workflow.is_file())
        data=workflow.read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:",data)
        self.assertIn("23 */4 * * *",data)
        self.assertIn("git add output/*.json data/discovered_repos.json data/snapshot.json",data)


if __name__ == "__main__":
    unittest.main()
