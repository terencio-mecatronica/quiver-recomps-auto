#!/usr/bin/env python3
"""Gerador independente de listas Quiver via .recomp.json, GitHub e GitLab.

Nenhuma consulta ao banco de dados recomp.fyi. O formato de manifesto e aberto
(CC BY 4.0); os registros sao obtidos diretamente dos repositorios originais.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

BASE = Path(__file__).resolve().parents[1]
WIN_STATUSES = {"playable", "released", "complete"}
DEV_STATUSES = {"exploring", "in-progress", "paused"}
VALID_TYPES = {"recomp", "port", "decomp"}
WIN_NAME = re.compile(r"(?:windows?|win[-_.]?(?:32|64|x64|x86|arm64)|x64[-_.]?win|x86[-_.]?win)", re.I)
ASSET_EXTENSION = re.compile(r"\.(?:zip|7z|rar|exe|msi|msix|msixbundle|tar\.gz)$", re.I)
EXCLUDED_ASSET = re.compile(r"(?:source|src|symbols?|debug|sdk|linux|macos|darwin|android|\.appimage)", re.I)
REPO = re.compile(r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+$")


class FetchFailure(RuntimeError):
    pass


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_if_changed(path: Path, content: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(content, indent=2, ensure_ascii=False, sort_keys=False) + "\n"
    if not path.exists() or path.read_text(encoding="utf-8") != serialized:
        path.write_text(serialized, encoding="utf-8")


class API:
    def __init__(self, github_token: str = "", gitlab_token: str = ""):
        self.github_token = github_token
        self.gitlab_token = gitlab_token

    def get(self, url: str, *, missing_ok: bool = False) -> Any:
        # Apenas hosts predefinidos; nunca seguir URLs fornecidas por manifestos.
        if not (url.startswith("https://api.github.com/") or
                url.startswith("https://gitlab.com/api/v4/")):
            raise ValueError("Host de API nao permitido")
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "quiver-recomps-auto/1.0"}
        if url.startswith("https://api.github.com/") and self.github_token:
            headers["Authorization"] = f"Bearer {self.github_token}"
            headers["X-GitHub-Api-Version"] = "2022-11-28"
        if url.startswith("https://gitlab.com/api/v4/") and self.gitlab_token:
            headers["PRIVATE-TOKEN"] = self.gitlab_token
        req = Request(url, headers=headers)
        try:
            with urlopen(req, timeout=18) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise FetchFailure(f"Resposta excede 2 MB: {url.split('?')[0]}")
                return json.loads(raw)
        except HTTPError as ex:
            if ex.code == 404 and missing_ok:
                return None
            # Para manifestos ausentes GitLab usa 404. As demais falhas
            # interrompem a publicacao para preservar a ultima lista boa.
            raise FetchFailure(f"HTTP {ex.code} em {url.split('?')[0]}") from ex
        except (URLError, TimeoutError, ValueError) as ex:
            raise FetchFailure(f"Erro ao consultar {url.split('?')[0]}: {ex}") from ex

    def get_text(self, url: str, *, missing_ok: bool = False) -> str | None:
        if not url.startswith("https://gitlab.com/api/v4/"):
            raise ValueError("Host de API nao permitido")
        headers = {"User-Agent": "quiver-recomps-auto/1.0"}
        if self.gitlab_token:
            headers["PRIVATE-TOKEN"] = self.gitlab_token
        try:
            with urlopen(Request(url, headers=headers), timeout=18) as r:
                raw = r.read(100_001)
                if len(raw) > 100_000:
                    raise FetchFailure("Manifesto excede 100 KB")
                return raw.decode("utf-8")
        except HTTPError as ex:
            if ex.code == 404 and missing_ok:
                return None
            raise FetchFailure(f"HTTP {ex.code} no manifesto GitLab") from ex
        except (URLError, TimeoutError, UnicodeError) as ex:
            raise FetchFailure(f"Erro no manifesto GitLab: {ex}") from ex


def github_api(repo: str, suffix: str = "") -> str:
    return f"https://api.github.com/repos/{repo}{suffix}"


def discover_github(api: API, max_pages: int) -> list[dict]:
    """Busca somente nomes de repositorios que anunciam arquivo de manifesto.
    A busca de codigo da API GitHub requer autenticacao. Paginacao limitada.
    """
    found = set()
    for filename in (".recomp.json", ".recomp-board.json"):
        for page in range(1, max_pages + 1):
            params = urlencode({"q": f"filename:{filename}", "per_page": 100, "page": page})
            body = api.get(f"https://api.github.com/search/code?{params}")
            items = body.get("items", [])
            for item in items:
                repo = (item.get("repository") or {}).get("full_name", "")
                if REPO.fullmatch(repo) and item.get("path") in (".recomp.json", ".recomp-board.json"):
                    found.add(repo)
            if len(items) < 100:
                break
    return [{"host": "github", "repo": r} for r in sorted(found, key=str.casefold)]


def metadata(api: API, host: str, repo: str) -> dict | None:
    if host == "github":
        return api.get(github_api(repo), missing_ok=True)
    return api.get(f"https://gitlab.com/api/v4/projects/{quote(repo, safe='')}", missing_ok=True)


def manifest(api: API, host: str, repo: str, default_branch: str) -> dict | None:
    for filename in (".recomp.json", ".recomp-board.json"):
        if host == "github":
            endpoint = github_api(repo, f"/contents/{filename}?ref={quote(default_branch, safe='')}")
            entry = api.get(endpoint, missing_ok=True)
            if entry is None:
                continue
            if entry.get("encoding") != "base64" or not isinstance(entry.get("content"), str):
                raise FetchFailure(f"Manifesto GitHub nao e base64: {repo}")
            try:
                raw = base64.b64decode(entry["content"], validate=False)
                if len(raw) > 100_000:
                    raise FetchFailure(f"Manifesto excede 100 KB: {repo}")
                body = json.loads(raw)
            except (ValueError, UnicodeDecodeError) as ex:
                raise FetchFailure(f"JSON invalido no manifesto: {repo}") from ex
        else:
            path = quote(filename, safe="")
            ref = quote(default_branch, safe="")
            raw = api.get_text(f"https://gitlab.com/api/v4/projects/{quote(repo, safe='')}/repository/files/{path}/raw?ref={ref}", missing_ok=True)
            if raw is None:
                continue
            try:
                body = json.loads(raw)
            except ValueError as ex:
                raise FetchFailure(f"JSON invalido no manifesto: {repo}") from ex
        if not isinstance(body, dict):
            raise FetchFailure(f"Manifesto nao e objeto JSON: {repo}")
        return body
    return None


def releases(api: API, host: str, repo: str) -> list:
    if host == "github":
        response = api.get(github_api(repo, "/releases?per_page=8"), missing_ok=True)
    else:
        response = api.get(f"https://gitlab.com/api/v4/projects/{quote(repo, safe='')}/releases?per_page=8", missing_ok=True)
    return response or []


def asset_names(release: dict, host: str) -> list[str]:
    if host == "github":
        return [a.get("name", "") for a in release.get("assets", [])
                if isinstance(a, dict) and a.get("browser_download_url", "").startswith("https://")]
    return [a.get("name", "") for a in (release.get("assets") or {}).get("links", [])
            if isinstance(a, dict) and str(a.get("url", "")).startswith("https://")]


def has_windows_asset(release_list: list, host: str) -> tuple[bool, list[str]]:
    for release in release_list:
        if host == "github" and (release.get("draft") or release.get("prerelease")):
            continue
        candidates = [n for n in asset_names(release, host)
                      if WIN_NAME.search(n) and ASSET_EXTENSION.search(n)
                      and not EXCLUDED_ASSET.search(n)]
        if candidates:
            return True, candidates
    return False, []


def types(value: Any) -> list[str]:
    vals = value if isinstance(value, list) else [value]
    return [v for v in vals if isinstance(v, str) and v in VALID_TYPES]


def windows_target(value: Any) -> bool:
    vals = value if isinstance(value, list) else [value]
    return any(str(t).strip().casefold() in ("windows", "windows 10", "windows 11", "win32", "win64") for t in vals)


def format_slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")[:90]


def make_entry(host: str, repo: str, record: dict, meta: dict, overrides: dict) -> tuple[dict | None, str, dict]:
    source_manifest = record.get("manifest")
    seed = record.get("seed") or {}
    manifest_used = isinstance(source_manifest, dict)
    m = source_manifest or {}
    name = str(m.get("game") or seed.get("name") or "").strip()[:160]
    status = str(m.get("status") or (seed.get("curatedStatus") if not manifest_used else "") or "").lower().strip()
    t = types(m.get("type") if manifest_used else seed.get("kind"))
    # Para descobrir repositorios nao curados, exigir autoidentificacao minima.
    valid_identity = bool(name and t and (seed or (m.get("game") and m.get("system") and m.get("type"))))
    windows = windows_target(m.get("targets")) if manifest_used else bool(seed.get("curatedWindows"))
    ok_asset, matched = has_windows_asset(record.get("releases", []), host)
    category = "ignored"
    if valid_identity and status in WIN_STATUSES and windows and ok_asset and any(k in ("recomp", "port") for k in t):
        category = "playable"
    elif valid_identity and status in DEV_STATUSES:
        category = "development"
    # Sem status declarado ou sem pacote Windows: nao inferir jogabilidade nem desenvolvimento.
    info = {"host": host, "repo": repo, "status": status or "not-reported", "type": t,
            "manifest_from_author": manifest_used, "declared_windows": windows,
            "windows_asset_detected": ok_asset, "matched_release_assets": matched,
            "classification": category,
            "evidence": f"https://{('github.com' if host == 'github' else 'gitlab.com')}/{repo}"}
    if category == "ignored":
        return None, category, info
    folder = format_slug(f"Recomp-{host}-{repo.replace('/', '-')}")
    tags = ["recomp-tracker", *t, "windows" if category == "playable" else "em-desenvolvimento", status]
    if category == "development" and not windows:
        tags.append("windows-nao-confirmado")
    tags = list(dict.fromkeys(tags))
    entry = {"name": name, "project": str(m.get("project") or seed.get("project") or repo.split("/")[-1])[:100],
             "repository": repo, "folderName": folder, "tags": tags,
             "appIconUrl": None}
    if host == "gitlab":
        entry["repositorySource"] = "gitlab"
    custom = overrides.get(f"{host}:{repo}", {})
    if isinstance(custom.get("appIconUrl"), str) and custom["appIconUrl"].startswith("https://"):
        entry["appIconUrl"] = custom["appIconUrl"]
    if isinstance(custom.get("releaseAssetFilter"), str) and custom["releaseAssetFilter"].strip():
        entry["releaseAssetFilter"] = custom["releaseAssetFilter"].strip()
    # Outros metadados NAO sao incluidos no JSON, para nao depender de campos
    # nao documentados pelo Quiver.
    return entry, category, info


def dedupe_entries(entries: list[dict]) -> list[dict]:
    results, repos, folders = [], set(), set()
    for entry in entries:
        key = (entry.get("repositorySource", "github").casefold(), entry.get("repository", "").casefold())
        folder = entry.get("folderName", "").casefold()
        if key in repos or folder in folders:
            continue
        repos.add(key)
        folders.add(folder)
        results.append(entry)
    return results


def regenerate(api: API, base: Path, *, discovery: bool = True) -> dict:
    config = read_json(base / "config/sources.json")
    overrides = read_json(base / "config/overrides.json")
    excluded = set(overrides.get("excludedRepositories", []))
    cached = read_json(base / "data/discovered_repos.json").get("repositories", [])
    seeds = config.get("repositories", [])
    discovered = []
    if discovery and config.get("autoDiscoverGitHubManifests"):
        if not api.github_token:
            print("AVISO: sem GITHUB_TOKEN; descoberta global nao executada", file=sys.stderr)
        else:
            # Uma falha na descoberta nao bloqueia a atualizacao dos repositorios
            # ja selecionados. O cache preserva descobertas anteriores.
            try:
                discovered = discover_github(api, max(1, min(5, int(config.get("autoDiscoverMaxPages", 2)))))
            except FetchFailure as ex:
                print(f"AVISO: descoberta indisponivel; usando cache: {ex}", file=sys.stderr)
    candidates = {}
    for c in [*cached, *discovered, *seeds]:
        host, repo = c.get("host", "github"), c.get("repo", "")
        if host not in ("github", "gitlab") or not REPO.fullmatch(repo):
            raise ValueError(f"Repositorio invalido ou nao suportado: {host}:{repo}")
        if host == "github" and repo.count("/") != 1:
            raise ValueError(f"Repositorio GitHub invalido: {repo}")
        key = f"{host}:{repo}"
        if key not in excluded:
            candidates[key] = {**candidates.get(key, {}), **c}
    # Atualizacao incremental: nao repete centenas de chamadas na mesma execucao.
    # Entradas ainda nao visitadas permanecem no ultimo snapshot conhecido.
    snapshot_path = base / "data/snapshot.json"
    snapshot = read_json(snapshot_path) if snapshot_path.exists() else {"cursor": 0, "repositories": {}}
    previous = {k: v for k, v in snapshot.get("repositories", {}).items() if k in candidates}
    all_keys = sorted(candidates)
    max_per_run = max(1, min(300, int(config.get("maxRepositoriesPerRun", 60))))
    cursor = int(snapshot.get("cursor", 0))
    start = cursor % len(all_keys) if all_keys else 0
    picked = [all_keys[(start + i) % len(all_keys)] for i in range(min(max_per_run, len(all_keys)))] if all_keys else []
    next_cursor = (start + len(picked)) % len(all_keys) if all_keys else 0
    discovered_valid = []
    seed_keys = {f"{s.get('host', 'github')}:{s.get('repo', '')}" for s in seeds}
    for key in picked:
        c = candidates[key]
        host, repo = c["host"], c["repo"]
        meta = metadata(api, host, repo)
        if meta is None:
            previous.pop(key, None)
            continue
        branch = meta.get("default_branch") or "main"
        own_manifest = manifest(api, host, repo, branch)
        if own_manifest is None and key not in seed_keys:
            previous.pop(key, None)
            continue
        if own_manifest is not None:
            discovered_valid.append({"host": host, "repo": repo})
        release_list = releases(api, host, repo)
        entry, category, info = make_entry(host, repo,
            {"seed": c if c.get("name") else {}, "manifest": own_manifest, "releases": release_list},
            meta, overrides.get("repositories", {}))
        previous[key] = {"classification": category, "entry": entry, "audit": info}
    playable = [rec["entry"] for key, rec in sorted(previous.items())
                if rec.get("classification") == "playable" and rec.get("entry")]
    developing = [rec["entry"] for key, rec in sorted(previous.items())
                  if rec.get("classification") == "development" and rec.get("entry")]
    playable = dedupe_entries(sorted(playable, key=lambda x: (x["name"].casefold(), x["repository"].casefold())))
    developing = dedupe_entries(sorted(developing, key=lambda x: (x["name"].casefold(), x["repository"].casefold())))
    audit = [rec["audit"] for _, rec in sorted(previous.items())]
    pending = [k for k in all_keys if k not in previous]
    # Base antiga preservada SOMENTE no combinado. A lista de jogaveis nao
    # herda emuladores e outras entradas sem validacao de status de recomp.
    legacy = read_json(base / "config/minha-lista-114.json").get("apps", [])
    combined = dedupe_entries([*playable, *developing, *legacy])
    payloads = {
        "windows-jogaveis.json": {"apps": playable},
        "em-desenvolvimento.json": {"apps": developing},
        "tudo-quiver.json": {"apps": combined},
        "relatorio.json": {"summary": {"playable_windows": len(playable),
                                       "development": len(developing),
                                       "combined_with_legacy": len(combined),
                                       "tracked": len(audit),
                                       "awaiting_first_check": len(pending),
                                       "checked_this_run": len(picked),
                                       "ignored": sum(i.get("classification") not in ("playable", "development") for i in audit)},
                           "projects": audit,
                           "not_yet_checked_repositories": pending}
    }
    # Apenas apos TODAS as consultas obrigatorias completas publicamos dados.
    for filename, body in payloads.items():
        write_json_if_changed(base / "output" / filename, body)
    write_json_if_changed(snapshot_path, {"cursor": next_cursor, "repositories": dict(sorted(previous.items()))})
    retained = {(x["host"], x["repo"]) for x in cached}
    retained |= {(x["host"], x["repo"]) for x in discovered_valid}
    retained |= {(x["host"], x["repo"]) for x in discovered}
    write_json_if_changed(base / "data/discovered_repos.json",
                          {"repositories": [{"host": host, "repo": repo} for host, repo in sorted(retained)]})
    return payloads["relatorio.json"]["summary"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Atualiza as listas Quiver com dados dos repositorios originais")
    parser.add_argument("--no-discovery", action="store_true", help="Somente repositorios configurados + cache")
    args = parser.parse_args()
    try:
        info = regenerate(API(os.getenv("GITHUB_TOKEN", ""), os.getenv("GITLAB_TOKEN", "")), BASE,
                          discovery=not args.no_discovery)
    except (FetchFailure, OSError, ValueError, KeyError, TypeError) as ex:
        print(f"ERRO: publicacao cancelada; arquivos anteriores preservados. {ex}", file=sys.stderr)
        return 1
    print(json.dumps(info, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
