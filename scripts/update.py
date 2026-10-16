#!/usr/bin/env python3
"""Quiver Recomps Auto v3.3: fallback SteamGridDB seguro, cache e buscas a cada 4h.

Nenhuma consulta ao banco de dados recomp.fyi. O formato de manifesto e aberto
(CC BY 4.0); os registros sao obtidos diretamente dos repositorios originais.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone
import re
import sys
import unicodedata
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, unquote, urlsplit
from urllib.request import Request, urlopen

__version__ = "3.3"
BASE = Path(__file__).resolve().parents[1]
WIN_STATUSES = {"playable", "released", "complete"}
DEV_STATUSES = {"exploring", "in-progress", "paused"}
VALID_TYPES = {"recomp", "port", "decomp"}
WIN_NAME = re.compile(r"(?:windows?|win[-_.]?(?:32|64|x64|x86|arm64)|x64[-_.]?win|x86[-_.]?win)", re.I)
PC_BUILD_NAME = re.compile(r"(?:^|[^a-z0-9])(?:pc|desktop)(?:[^a-z0-9]|$)", re.I)
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



# Termos restringidos a recompilações e ports. Não copiamos catálogos de terceiros.
DEFAULT_REPOSITORY_QUERIES = [
    '"static recompilation" in:description',
    '"recompilation" "Windows" in:description',
    '"recompilation" "native" in:description',
    '"native PC port" in:description',
    '"source port" in:description',
    'recomp in:name',
    'topic:static-recompilation',
    'topic:n64recomp',
    '"N64Recomp" in:readme',
    '"static recompilation" in:readme',
]


def discover_repository_search(api: API, queries: list[str], max_pages: int = 2,
                               per_page: int = 100, *, sort: str | None = "updated",
                               page_window: int = 0) -> tuple[list[dict], dict]:
    """Busca ampla e incremental na API oficial de repositorios.

    Primeira pagina sempre lida; paginas seguintes rotacionam em janelas para
    ampliar cobertura ao longo das execucoes sem estourar 30 buscas/minuto.
    Alternar 'updated' e relevancia tambem evita vies de um unico ranking.
    Um erro apos retornos parciais preserva esses resultados e e auditado.
    """
    if sort not in (None, "updated") or max_pages < 1 or not 1 <= per_page <= 100:
        raise ValueError("Parametros invalidos de pesquisa GitHub")
    window = max(0, page_window) % 5
    pages = [1] + [2 + window * max(0, max_pages - 1) + i
                   for i in range(max(0, max_pages - 1))]
    # A busca do GitHub entrega no maximo os primeiros 1000 resultados.
    pages = [p for p in pages if p * per_page <= 1000]
    discovered: dict[str, dict] = {}
    summary = {"queries": len(queries), "requests": 0, "incomplete_queries": [],
               "returned": 0, "sort": sort or "best-match", "pages": pages}
    for query in queries:
        if not isinstance(query, str) or not query.strip() or len(query) > 250:
            raise ValueError("Consulta de descoberta invalida")
        for page in pages:
            params = {"q": query, "per_page": per_page, "page": page}
            if sort:
                params.update(sort=sort, order="desc")
            url = "https://api.github.com/search/repositories?" + urlencode(params)
            try:
                response = api.get(url)
            except FetchFailure as ex:
                if summary["requests"] == 0:
                    raise
                summary["error"] = str(ex)
                summary["incomplete_queries"].append(query)
                return sorted(discovered.values(), key=lambda x: x["repo"].casefold()), summary
            summary["requests"] += 1
            if response.get("incomplete_results"):
                summary["incomplete_queries"].append(query)
            rows = response.get("items", [])
            if not isinstance(rows, list):
                raise FetchFailure("Resposta invalida da pesquisa de repositorios")
            summary["returned"] += len(rows)
            for row in rows:
                if not isinstance(row, dict) or row.get("private") or row.get("archived") or row.get("fork"):
                    continue
                name = row.get("full_name", "")
                if not isinstance(name, str) or name.count("/") != 1 or not REPO.fullmatch(name):
                    continue
                key = name.casefold()
                if key not in discovered:
                    discovered[key] = {"host": "github", "repo": name, "discoveredBy": "repository_search"}
                pushed_at = row.get("pushed_at") or row.get("updated_at")
                if isinstance(pushed_at, str) and pushed_at > discovered[key].get("lastPush", ""):
                    discovered[key]["lastPush"] = pushed_at
                matches = discovered[key].setdefault("matchedQueries", [])
                if query not in matches:
                    matches.append(query)
            if len(rows) < per_page:
                break
    return sorted(discovered.values(), key=lambda x: x["repo"].casefold()), summary


def get_readme(api: API, repo: str) -> str:
    """Readme obtido da API oficial; tamanho limitado; 404 é normal."""
    obj = api.get(github_api(repo, "/readme"), missing_ok=True)
    if not isinstance(obj, dict) or obj.get("encoding") != "base64":
        return ""
    contents = obj.get("content", "")
    if not isinstance(contents, str) or len(contents) > 170_000:
        return ""
    try:
        raw = base64.b64decode(contents, validate=False)
        return raw[:95_000].decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


# Somente metadados escritos pelos autores. Palavras em buscas não bastam.
RECOMP_CLAIM = re.compile(r"\b(?:static(?:ally)?[ -]?recompil(?:ation|ed|ing)|"
                          r"recompil(?:ation|ed|ing) of|"
                          r"(?:n64|gamecube|switch|ps[12])[ -]?recomp(?:ilation)?|"
                          r"game[ -]?recompil(?:ation|ation project))\b", re.I)
PORT_CLAIM = re.compile(r"\b(?:source[ -]port|native[ -](?:pc|windows)[ -]port|"
                        r"(?:pc|windows)[ -]port of|port of (?:the )?game)\b", re.I)
DEV_CLAIM = re.compile(r"\b(?:work in progress|under development|"
                       r"still in development|early development|in active development|"
                       r"currently in development|currently working on|"
                       r"experimental (?:build|project|recompilation)|"
                       r"proof of concept|pre[- ]alpha|not playable yet|"
                       r"in[- ]progress|w\.?i\.?p\.?)\b", re.I)
NON_GAME_CLAIM = re.compile(r"^(?:a |an |the )?(?:toolchain|compiler|framework|sdk|"
                            r"recompilation tool|recompilation framework|"
                            r"tutorial|template|library for|example project)\b", re.I)


def classify_self_description(meta: dict, readme: str) -> tuple[str | None, str]:
    """Atribui apenas o tipo de trabalho com evidência textual próxima do início.

    Evita confundir frameworks de recompilação com jogos recompilados.
    """
    description = str(meta.get("description") or "")[:1000]
    readme_opening = re.sub(r"<[^>]*>", " ", readme[:2600])
    candidate = description + "\n" + readme_opening
    if NON_GAME_CLAIM.search(description):
        return None, "Descrição do autor indica ferramenta ou biblioteca"
    kind = "recomp" if RECOMP_CLAIM.search(candidate) else "port" if PORT_CLAIM.search(candidate) else None
    if kind is None:
        return None, "Sem autoidentificação de recompilação/port pelo autor"
    return kind, "Declaração explícita no README/descrição do repositório"


def classify_unmanifested(repo: str, meta: dict, readme: str,
                          release_list: list[dict]) -> tuple[dict | None, str, dict]:
    """Decisões verificáveis, sem assumir que uma release é plenamente jogável.

    Build distribuída para Windows -> lista 'Windows'; sem build, desenvolvimento
    somente se o autor explicitamente diz WIP. Demais casos vão para revisão.
    """
    kind, evidence = classify_self_description(meta, readme)
    author_text = str(meta.get("description") or "") + "\n" + readme[:2600]
    declared_windows = bool(re.search(r"\bwindows\b", author_text, re.I))
    windows_asset, asset_names_found = has_windows_asset(
        release_list, "github", pc_is_windows=declared_windows)
    dev = bool(DEV_CLAIM.search(author_text))
    name = str(meta.get("name") or repo.split("/")[-1]).strip()[:160]
    reason = evidence
    stage = "review"
    if kind and windows_asset:
        stage = "playable"
        reason = "Release não-draft contém pacote explicitamente nomeado para Windows (pode ser beta)"
    elif kind and dev:
        stage = "development"
        reason = "Autor declarou explicitamente que o projeto está em desenvolvimento"
    elif kind:
        reason = "Tipo confirmado, mas sem pacote Windows nem estado de desenvolvimento explícito"
    result = None
    if stage != "review":
        status = "windows-build" if stage == "playable" else "in-progress"
        tags = ["recomp-tracker", kind, "windows" if stage == "playable" else "em-desenvolvimento", status]
        if stage == "playable":
            if any(r.get("prerelease") for r in release_list if not r.get("draft")):
                tags.append("pode-ser-beta")
        else:
            tags.append("windows-nao-confirmado")
        result = {"name": name, "project": name,
                  "repository": repo, "folderName": format_slug("Recomp-github-" + repo.replace("/", "-")),
                  "tags": tags, "appIconUrl": None}
    audit = {"host": "github", "repo": repo, "classification": stage,
             "manifest_from_author": False, "type": [kind] if kind else [],
             "status": "windows-build" if stage == "playable" else "in-progress" if dev else "not-reported",
             "declared_windows": declared_windows,
             "windows_asset_detected": windows_asset,
             "matched_release_assets": asset_names_found,
             "reason": reason, "evidence": "https://github.com/" + repo,
             "evidence_urls": ["https://github.com/" + repo,
                               "https://github.com/" + repo + "/releases"]}
    return result, stage, audit


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


def has_windows_asset(release_list: list, host: str, *, pc_is_windows: bool = False) -> tuple[bool, list[str]]:
    """Pacote Windows explicito, ou PC com Windows declarado pelo proprio autor.

    'PC' sozinho nao basta: poderia ser um port exclusivo para Linux/macOS.
    """
    for release in release_list:
        if host == "github" and release.get("draft"):
            continue
        candidates = [n for n in asset_names(release, host)
                      if (WIN_NAME.search(n) or (pc_is_windows and PC_BUILD_NAME.search(n)))
                      and ASSET_EXTENSION.search(n) and not EXCLUDED_ASSET.search(n)]
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


# Fontes que ja vinham como capas de jogos no arquivo base de 114 itens.
# Nao reaproveitamos avatares de perfis do GitHub automaticamente.
LEGACY_ART_DOMAINS = {
    "cdn.cloudflare.steamstatic.com",
    "cdn.akamai.steamstatic.com",
    "shared.fastly.steamstatic.com",
    "steamcdn-a.akamaihd.net",
}
LEGACY_APPROVED_URLS = {
    "https://raw.githubusercontent.com/yairm210/Unciv/master/docs/assets/Icon.png",
}


def validate_icon_url(value: Any, key: str) -> str | None:
    """Aceita somente URL de imagem HTTPS preenchida manualmente, ou null.

    Nao faz requisicoes de rede: nenhum token ou arquivo local fica exposto.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Imagem de {key}: use uma URL HTTPS ou null")
    value = value.strip()
    from urllib.parse import urlsplit
    parsed = urlsplit(value)
    if (len(value) > 2048 or parsed.scheme != "https" or not parsed.hostname
            or parsed.username or parsed.password):
        raise ValueError(f"Imagem de {key}: URL HTTPS invalida")
    return value


def legacy_image_approved(value: Any) -> bool:
    """Somente artes previamente curadas (nao avatares de perfil)."""
    if not isinstance(value, str):
        return False
    from urllib.parse import urlsplit
    parsed = urlsplit(value)
    return parsed.scheme == "https" and (
        parsed.hostname in LEGACY_ART_DOMAINS or value in LEGACY_APPROVED_URLS
    )


def curated_image(host: str, repo: str, fallback: Any, settings: dict) -> str | None:
    """Prioridade: imagem escolhida > override antigo > arte confiavel da base > neutro.

    'null' explicito nos overrides significa remover a imagem. Os icones de
    snapshots antigos nao sao usados como fonte automatica.
    """
    full_key = f"{host}:{repo}"
    images = settings.get("images", {})
    if not isinstance(images, dict):
        raise ValueError("config/overrides.json: images deve ser objeto")
    repository_options = settings.get("repositories", {})
    if not isinstance(repository_options, dict):
        raise ValueError("config/overrides.json: repositories deve ser objeto")
    for key in (full_key, repo):
        if key in images:
            return validate_icon_url(images[key], key)
    for key in (full_key, repo):
        options = repository_options.get(key)
        if isinstance(options, dict) and "appIconUrl" in options:
            return validate_icon_url(options["appIconUrl"], key)
    if legacy_image_approved(fallback):
        return fallback
    return None


def with_curated_images(entries: list[dict], settings: dict, *, legacy: bool = False) -> list[dict]:
    result = []
    for original in entries:
        entry = original.copy()
        host = entry.get("repositorySource", "github")
        repo = entry.get("repository", "")
        # Para repositórios detectados online não confiar em URLs antigas no snapshot.
        fallback = entry.get("appIconUrl") if legacy else None
        entry["appIconUrl"] = curated_image(host, repo, fallback, settings)
        result.append(entry)
    return result



# So usamos imagens publicadas no repositorio do proprio autor.
# Evitamos imagens genericas de HUD/screenshots, perfis pessoais e fornecedores.
ARTWORK_SCANNER_VERSION = 2  # invalida apenas caches negativos antigos
ARTWORK_EXT = re.compile(r"\.(?:png|jpe?g|webp)$", re.I)
ARTWORK_DIRS = {"assets", "art", "artwork", "images", "img", "media", "resources",
                "resource", "res", "branding", "docs", "public", ".github",
                "icons", "icon", "dist"}
ARTWORK_EXCLUDED = {"third_party", "third-party", "vendor", "external", "node_modules",
                    "test", "tests", "examples", "samples", "dependencies", "deps", "packages",
                    "screenshots", "screenshot", "thirdparty", "lib", "libs"}
ARTWORK_PRIORITIES = {
    "cover": 21, "coverart": 20, "capsule": 19, "keyart": 18, "boxart": 17,
    "poster": 16, "banner": 15, "header": 14, "hero": 13, "splash": 12,
    "artwork": 11, "logo": 9, "icon": 7,
}


def artwork_explicitly_configured(entry: dict, overrides: dict) -> bool:
    repo = entry.get("repository", "")
    host = entry.get("repositorySource", "github")
    for key in (f"{host}:{repo}", repo):
        if key in overrides.get("images", {}):
            return True  # inclusive null = remocao solicitada
        override = overrides.get("repositories", {}).get(key)
        if isinstance(override, dict) and "appIconUrl" in override:
            return True
    return False


def art_file(path: str, size: Any, *, repo: str = "") -> tuple[int, str] | None:
    """Retorna score e caminho apenas para uma provavel arte de projeto.

    Em monorepos, nomes genericos em pastas internas sem identidade do projeto
    sao ignorados. Imagens do jogo nao sao inferidas de nomes aleatorios.
    """
    if (not isinstance(path, str) or not isinstance(size, int)
            or not 1024 <= size <= 8_000_000 or len(path) > 240
            or not ARTWORK_EXT.search(path)):
        return None
    parts = path.split("/")
    if (not parts or any(part in ("", ".", "..") for part in parts)
            or any(part.lower() in ARTWORK_EXCLUDED for part in parts[:-1])):
        return None
    dirs = [x.lower() for x in parts[:-1]]
    file = parts[-1].lower()
    stem = ARTWORK_EXT.sub("", file)
    compact = re.sub(r"[^a-z0-9]", "", stem)
    # Somente um token visual completo: "scoreboard" nao e box art,
    # "login" nao e logo, "radar-icon" pode ser arte de interface.
    tokens = [t for t in re.split(r"[-_. ]+", stem) if t]
    score = 0
    aliases = {"cover-art": "coverart", "box-art": "boxart", "key-art": "keyart"}
    for tok in tokens:
        score = max(score, ARTWORK_PRIORITIES.get(aliases.get(tok, tok), 0))
    for kind, priority in ARTWORK_PRIORITIES.items():
        if compact == kind or stem.startswith(kind + "-") or stem.startswith(kind + "_"):
            score = max(score, priority)
    project = repo.split("/")[-1].lower() if repo else ""
    project_compact = re.sub(r"[^a-z0-9]", "", project)
    project_tokens = [x for x in re.split(r"[^a-z0-9]+", project) if len(x) >= 5]
    # So confiar no prefixo de projeto quando ha coincidencia clara.
    project_match = bool(project and (
        (len(project_compact) >= 5 and project_compact in compact)
        or any(x in compact for x in project_tokens)
    ))
    path_project_match = bool(project and any(
        re.sub(r"[^a-z0-9]", "", x) == project_compact for x in dirs))
    in_art_dir = bool(dirs and dirs[0] in ARTWORK_DIRS)
    nested_author_art = bool(dirs and dirs[-1] in ARTWORK_DIRS and path_project_match)
    if not in_art_dir and not nested_author_art and len(dirs) != 0:
        return None
    # Evitar "health-icon.png" e elementos da interface. Aceitar por nome
    # especifico do projeto ou genericos oficiais (app/game/project/launcher).
    generic = stem in {"app", "application", "game", "project", "launcher", "main"}
    if score == 0:
        if project_match and (in_art_dir or nested_author_art):
            score = 6  # ex.: dist/azahar.png
        elif generic and dirs and dirs[-1] in {"icons", "icon", "branding", "assets", "dist", "resources", "resource", "res"}:
            score = 6  # ex.: icons/app.png
        else:
            return None
    if score <= 9 and not (project_match or path_project_match or stem in {
        "logo", "icon", "app", "app-icon", "app_icon", "game-icon", "game_icon",
        "application-icon", "project-icon", "launcher-icon", "main-icon",
        "project-logo", "app-logo", "game-logo", "launcher-logo"
    }):
        return None
    if stem in {"icon", "logo"} and len(dirs) >= 3 and not path_project_match:
        return None
    # Foto real do autor, nao screenshots. Capa ganha de logo e icone.
    return (score * 100 + (20 if project_match else 0)
            - len(parts) * 2, path)


def choose_author_art(tree: dict, repo: str = "") -> str | None:
    """Seleciona cover/banner/logo/icone validos publicados no proprio repo."""
    if not isinstance(tree, dict):
        return None
    best = (0, "")
    for item in tree.get("tree", []):
        if not isinstance(item, dict) or item.get("type") != "blob":
            continue
        candidate = art_file(item.get("path"), item.get("size"), repo=repo)
        if candidate and (candidate[0] > best[0] or
                          candidate[0] == best[0] and candidate[1].lower() < best[1].lower()):
            best = candidate
    return best[1] or None


def choose_readme_art(readme: str, tree: dict) -> str | None:
    """README explicito e arquivo existente sao mais confiaveis que heuristicas."""
    if not isinstance(readme, str) or not isinstance(tree, dict):
        return None
    files = {item.get("path"): item for item in tree.get("tree", [])
             if isinstance(item, dict) and item.get("type") == "blob"}
    matches = []
    # Markdown e HTML locais; URLs externas ou user uploads nao sao aceitos.
    matches.extend((m.group(1), m.group(2)) for m in re.finditer(
        r"!\[([^\]\n]{0,100})\]\(([^)\s]+)(?:\s+[^)]*)?\)", readme[:30000]))
    for m in re.finditer(r"<img\b[^>]{0,600}>", readme[:30000], re.I):
        tag = m.group(0)
        src = re.search(r"\bsrc\s*=\s*['\"]([^'\"]+)['\"]", tag, re.I)
        alt = re.search(r"\b(?:alt|title)\s*=\s*['\"]([^'\"]+)['\"]", tag, re.I)
        if src and alt:
            matches.append((alt.group(1), src.group(1)))
    hints = {"cover": 21, "key art": 18, "box art": 17, "capsule": 19,
             "banner": 15, "header": 14, "poster": 16, "hero": 13,
             "artwork": 11, "splash": 12, "logo": 9, "icon": 7}
    best = (0, "")
    for alt, value in matches:
        rank = max((score for label, score in hints.items() if label in alt.lower()), default=0)
        if not rank or value.startswith(("/", "http:", "https:", "data:", "//")):
            continue
        path = unquote(value.split("?", 1)[0].split("#", 1)[0]).removeprefix("./")
        parts = path.split("/")
        if (any(part in ("", ".", "..") for part in parts)
                or any(part.lower() in ARTWORK_EXCLUDED for part in parts[:-1])
                or not ARTWORK_EXT.search(path)):
            continue
        info = files.get(path)
        if not info or not isinstance(info.get("size"), int) or not 1024 <= info["size"] <= 8_000_000:
            continue
        if rank > best[0]:
            best = rank, path
    return best[1] or None


def discover_author_art(api: API, repo: str) -> str | None:
    """Metadados/README/arvore Git do autor (sem baixar arquivos binarios)."""
    if not REPO.fullmatch(repo) or repo.count("/") != 1:
        return None
    meta = api.get(github_api(repo), missing_ok=True)
    if not isinstance(meta, dict):
        return None
    branch = str(meta.get("default_branch") or "main")
    if len(branch) > 120 or branch.startswith("/") or ".." in branch:
        return None
    tree = api.get(github_api(repo, "/git/trees/" + quote(branch, safe="") + "?recursive=1"), missing_ok=True)
    if isinstance(tree, dict) and tree.get("truncated"):
        raise FetchFailure("Arvore Git incompleta; tenta novamente na proxima execucao")
    readme = get_readme(api, repo) if isinstance(tree, dict) else ""
    path = choose_readme_art(readme, tree) or choose_author_art(tree, repo=repo)
    if not path:
        return None
    safe_path = "/".join(quote(piece, safe="-._~") for piece in path.split("/"))
    return "https://raw.githubusercontent.com/" + repo + "/" + quote(branch, safe="") + "/" + safe_path


def populate_author_images(api: API, entries: list[dict], overrides: dict, cache: dict,
                           max_scans: int = 30, now: datetime | None = None) -> dict:
    """Cache por algoritmo, retomada incremental e recheck apos 14 dias."""
    now = now or datetime.now(timezone.utc)
    if not isinstance(cache, dict):
        raise ValueError("Cache de capas invalido")
    cutoff = now - timedelta(days=14)
    available = {}
    for entry in entries:
        repo = entry.get("repository", "")
        if (entry.get("repositorySource", "github") != "github" or
                not isinstance(repo, str) or repo.count("/") != 1 or
                not REPO.fullmatch(repo) or entry.get("appIconUrl") or
                artwork_explicitly_configured(entry, overrides)):
            continue
        available[repo] = True
    needs_check = []
    invalidated = 0
    for repo in sorted(available, key=str.casefold):
        cached = cache.get(repo)
        try:
            checked = datetime.fromisoformat(cached["checked_at"]) if isinstance(cached, dict) else None
        except (ValueError, TypeError, KeyError):
            checked = None
        negative_old = (isinstance(cached, dict) and cached.get("url") is None
                        and cached.get("scanner_version", 0) < ARTWORK_SCANNER_VERSION)
        if negative_old:
            invalidated += 1
        if checked is None or checked.tzinfo is None or checked < cutoff or negative_old:
            needs_check.append(repo)
    scanned = 0
    errors = 0
    rate_limited = False
    for repo in needs_check[:max(0, max_scans)]:
        try:
            url = discover_author_art(api, repo)
        except (FetchFailure, ValueError) as ex:
            errors += 1
            if "HTTP 429" in str(ex) or "HTTP 403" in str(ex):
                rate_limited = True
                break  # nao insistir quando a API limitar
            # Nao guardar erro de rede como prova de falta de imagem.
            continue
        cache[repo] = {"url": url, "checked_at": now.isoformat(),
                       "scanner_version": ARTWORK_SCANNER_VERSION}
        scanned += 1
    applied = 0
    for entry in entries:
        repo = entry.get("repository", "")
        if (repo in available and not entry.get("appIconUrl") and
                isinstance(cache.get(repo), dict) and cache[repo].get("url")):
            entry["appIconUrl"] = cache[repo]["url"]
            applied += 1
    return {"scanned": scanned, "applied": applied, "errors": errors,
            "rate_limited": rate_limited, "negative_cache_invalidated": invalidated,
            "still_to_scan": max(0, len(needs_check)-scanned),
            "remaining_without_image": sum(not x.get("appIconUrl") for x in entries)}



# SteamGridDB e apenas fallback. Nenhuma API key aparece em arquivos publicados.
# Referencia: https://www.steamgriddb.com/api/v2
SGDB_API_BASE = "https://www.steamgriddb.com/api/v2/"
SGDB_RESOLVER_VERSION = 1
SGDB_SKIP_TAGS = {"emulator", "tool", "launcher", "frontend", "modding", "library", "framework"}
SGDB_SUFFIX = re.compile(
    r"(?:[\s._:-]+(?:static[ -]?recomp(?:ilation)?|recompiled|recomp(?:iled)?|"
    r"decomp(?:ilation)?|source[ -]?port|native[ -]?port|pc[ -]?port|"
    r"windows[ -]?port|windows|win64|win32))+$", re.I)


class SteamGridDBFailure(RuntimeError):
    def __init__(self, code: int | None = None):
        self.code = code
        super().__init__(f"SteamGridDB indisponivel (HTTP {code})" if code else
                         "SteamGridDB indisponivel (rede)")


class SteamGridDBClient:
    """API de leitura SteamGridDB; a chave nunca e logada nem serializada."""
    def __init__(self, key: str):
        self._key = key.strip()
        if not self._key:
            raise ValueError("Chave SteamGridDB ausente")

    def get(self, path: str) -> dict | None:
        if not (path.startswith(("search/autocomplete/", "grids/game/")) and
                ".." not in path and len(path) <= 300):
            raise ValueError("Caminho SteamGridDB nao permitido")
        request = Request(SGDB_API_BASE + path,
                          headers={"Authorization": "Bearer " + self._key,
                                   "Accept": "application/json",
                                   "User-Agent": "quiver-recomps-auto/3.3"})
        try:
            with urlopen(request, timeout=15) as response:
                content = response.read(1_000_001)
                if len(content) > 1_000_000:
                    raise SteamGridDBFailure()
                value = json.loads(content)
                if not isinstance(value, dict):
                    raise SteamGridDBFailure()
                return value
        except HTTPError as exc:
            if exc.code == 404:
                return None
            raise SteamGridDBFailure(exc.code) from None
        except (URLError, TimeoutError, ValueError, UnicodeError):
            raise SteamGridDBFailure() from None


def sgdb_normalize(value: str) -> str:
    """Pontuacao e acentos nao influenciam match; sequencia de palavras sim."""
    text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    text = text.casefold().replace("&", " and ")
    return " ".join(re.findall(r"[a-z0-9]+", text))


def sgdb_title(entry: dict) -> str | None:
    """Nunca busca por fragmentos, siglas ou nomes genericos inferidos da franquia."""
    tags = {str(t).casefold() for t in entry.get("tags", [])}
    if tags.intersection(SGDB_SKIP_TAGS):
        return None
    title = entry.get("name")
    if not isinstance(title, str):
        return None
    title = title.strip()[:120]
    title = SGDB_SUFFIX.sub("", title).strip(" :-_.,")
    normalized = sgdb_normalize(title)
    if (len(normalized) < 6 or len(normalized) > 85 or not re.search(r"[a-z]", normalized)
            or normalized in {"recomp", "recompiled", "decomp", "launcher", "port"}):
        return None
    # Siglas sem titulo completo (Z3R, RE2 etc.) exigem mapeamento curado.
    if len(normalized.split()) == 1 and normalized.isupper():
        return None
    return title


def sgdb_image_url(value: Any) -> str | None:
    """URLs fornecidas pela API: restringir a servidores de arte conhecidos."""
    if not isinstance(value, str) or len(value) > 2048:
        return None
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    host = parsed.hostname.lower()
    if host == "s3.amazonaws.com":
        allowed = parsed.path.startswith("/steamgriddb/")
    else:
        allowed = host == "steamgriddb.com" or host.endswith(".steamgriddb.com")
    if not allowed or not re.search(r"\.(?:png|jpe?g|webp)(?:$|[?#])", value, re.I):
        return None
    return value


def sgdb_lookup(client: Any, title: str) -> tuple[str, str | None, int | None]:
    """Busca um unico jogo de nome exato e uma grid horizontal segura."""
    found = client.get("search/autocomplete/" + quote(title, safe=""))
    candidates = found.get("data", []) if isinstance(found, dict) else []
    if not isinstance(candidates, list):
        return "no_match", None, None
    normalized = sgdb_normalize(title)
    exact = {c["id"] for c in candidates
             if isinstance(c, dict) and isinstance(c.get("id"), int)
             and c["id"] > 0 and sgdb_normalize(str(c.get("name", ""))) == normalized}
    if not exact:
        return "no_match", None, None
    if len(exact) != 1:
        return "ambiguous", None, None
    game_id = next(iter(exact))
    path = (f"grids/game/{game_id}?dimensions=920x430,460x215&types=static"
            "&nsfw=false&humor=false&epilepsy=false&limit=30")
    images = client.get(path)
    grids = images.get("data", []) if isinstance(images, dict) else []
    if not isinstance(grids, list):
        return "no_art", None, game_id
    valid = [x for x in grids if isinstance(x, dict) and sgdb_image_url(x.get("url"))]
    if not valid:
        return "no_art", None, game_id
    best = max(valid, key=lambda x: (int(x.get("score") or 0), int(x.get("id") or 0)))
    return "success", sgdb_image_url(best["url"]), game_id


def populate_steamgrid_images(client: Any, entries: list[dict], overrides: dict,
                              author_cache: dict, sgdb_cache: dict,
                              *, max_lookups: int = 25,
                              now: datetime | None = None) -> dict:
    """Integra SGDB sem substituir artes dos autores, overrides ou resultados curados.

    Primeiro procura imagem no GitHub: so consulta SGDB apos ter checado esse
    repositorio e confirmado ausencia de arte local. Resultados positivos e
    negativos persistem no snapshot; falhas transitorias nao viram negativos.
    """
    stats = {"enabled": bool(client), "queries": 0, "matches": 0,
             "applied": 0, "ambiguous": 0, "no_match": 0, "no_art": 0,
             "errors": 0, "rate_limited": False, "pending": 0}
    if not client:
        return stats
    if not isinstance(sgdb_cache, dict) or not isinstance(author_cache, dict):
        raise ValueError("Cache SteamGridDB invalido")
    now = now or datetime.now(timezone.utc)
    expiration = now - timedelta(days=30)
    queue = []
    for entry in entries:
        repo = entry.get("repository", "")
        if (not isinstance(repo, str) or entry.get("repositorySource", "github") != "github"
                or entry.get("appIconUrl") or artwork_explicitly_configured(entry, overrides)):
            continue
        # Uma arte do autor tem prioridade mesmo que ainda nao tenha sido pesquisada.
        author = author_cache.get(repo)
        if not isinstance(author, dict) or author.get("url") is not None:
            continue
        title = sgdb_title(entry)
        if title is None:
            continue
        key = "github:" + repo
        previous = sgdb_cache.get(key)
        if (isinstance(previous, dict) and previous.get("version") == SGDB_RESOLVER_VERSION
                and previous.get("title") == sgdb_normalize(title)):
            if previous.get("status") == "success":
                url = sgdb_image_url(previous.get("url"))
                if url:
                    entry["appIconUrl"] = url
                    stats["applied"] += 1
                    continue
            try:
                checked = datetime.fromisoformat(previous["checked_at"])
                if checked.tzinfo is not None and checked >= expiration:
                    continue
            except (KeyError, ValueError, TypeError):
                pass
        queue.append((entry, key, title))
    stats["pending"] = len(queue)
    for entry, key, title in queue[:max(0, min(100, max_lookups))]:
        try:
            status, url, game_id = sgdb_lookup(client, title)
            stats["queries"] += 1
        except SteamGridDBFailure as exc:
            stats["errors"] += 1
            if exc.code in (401, 403, 429):
                stats["rate_limited"] = exc.code == 429
                stats["auth_error"] = exc.code in (401, 403)
                break
            continue
        if status == "success":
            entry["appIconUrl"] = url
            stats["matches"] += 1
            stats["applied"] += 1
        elif status in ("ambiguous", "no_match", "no_art"):
            stats[status] += 1
        sgdb_cache[key] = {"version": SGDB_RESOLVER_VERSION, "title": sgdb_normalize(title),
                           "status": status, "url": url if status == "success" else None,
                           "game_id": game_id, "checked_at": now.isoformat()}
    return stats

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
    if not isinstance(custom, dict):
        raise ValueError(f"Override invalido: {host}:{repo}")
    if isinstance(custom.get("releaseAssetFilter"), str) and custom["releaseAssetFilter"].strip():
        entry["releaseAssetFilter"] = custom["releaseAssetFilter"].strip()
    # Imagem aplicada na etapa final da montagem do JSON. Isso permite
    # aplicar mudancas de configuracao mesmo a projetos ainda no snapshot.
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


def regenerate(api: API, base: Path, *, discovery: bool = True, sgdb_client: Any = None) -> dict:
    config = read_json(base / "config/sources.json")
    overrides = read_json(base / "config/overrides.json")
    excluded = set(overrides.get("excludedRepositories", []))
    cached = read_json(base / "data/discovered_repos.json").get("repositories", [])
    seeds = config.get("repositories", [])
    snapshot_path = base / "data/snapshot.json"
    snapshot = read_json(snapshot_path) if snapshot_path.exists() else {"cursor": 0, "repositories": {}}
    search_cycle = int(snapshot.get("search_cycle", 0))
    manifest_retry_at = snapshot.get("manifest_retry_at", "")
    now = datetime.now(timezone.utc)
    search_attempted = False
    discovered = []
    search_stats = {"repository_search": "disabled"}
    if discovery and api.github_token:
        # Busca de repositórios primeiro: não depende de .recomp.json.
        if config.get("autoDiscoverGitHubRepositories", True):
            try:
                queries = config.get("repositorySearchQueries", DEFAULT_REPOSITORY_QUERIES)
                if not isinstance(queries, list) or len(queries) > 20:
                    raise ValueError("repositorySearchQueries deve ser uma lista com até 20 termos")
                search_attempted = True
                # V3.1: 2 paginas x 100 resultados por consulta, por padrao.
                # Executa 20 buscas quando ha 10 consultas; alterna ordenacao
                # por atividade e relevancia e varre paginas adicionais por ciclo.
                repo_results, search_stats = discover_repository_search(
                    api, queries, max(1, min(3, int(config.get("repositorySearchPages", 2)))),
                    max(10, min(100, int(config.get("repositorySearchPerPage", 100)))),
                    sort="updated" if search_cycle % 2 == 0 else None,
                    page_window=search_cycle // 2)
                discovered.extend(repo_results)
            except FetchFailure as ex:
                search_stats = {"error": str(ex)}
                print(f"AVISO: busca de repositórios indisponível: {ex}", file=sys.stderr)
        # 429 persistente em search/code: respeita um cooldown de 24h entre tentativas.
        if config.get("autoDiscoverGitHubManifests") and (not manifest_retry_at or
                manifest_retry_at <= now.isoformat()):
            try:
                discovered.extend(discover_github(api, max(1, min(3, int(config.get("autoDiscoverMaxPages", 1))))))
                manifest_retry_at = ""
            except FetchFailure as ex:
                if "HTTP 429" in str(ex) or "HTTP 403" in str(ex):
                    manifest_retry_at = (now + timedelta(hours=24)).isoformat()
                print(f"AVISO: busca de manifestos indisponível: {ex}", file=sys.stderr)
    elif discovery:
        print("AVISO: sem GITHUB_TOKEN; usando cache e fontes explícitas", file=sys.stderr)
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
    previous = {k: v for k, v in snapshot.get("repositories", {}).items() if k in candidates}
    all_keys = sorted(candidates, key=str.casefold)
    max_per_run = max(1, min(300, int(config.get("maxRepositoriesPerRun", 60))))
    cursor = int(snapshot.get("cursor", 0))
    start = cursor % len(all_keys) if all_keys else 0
    # V3.1: ate 2/3 das vagas para projetos sem primeira verificacao,
    # especialmente os atualizados recentemente. O restante mantem uma
    # rotacao justa que revisita projetos antigos e evita desatualizacao.
    pending_keys = [k for k in all_keys if k not in previous]
    # Datas ISO 8601 em ordem decrescente, repositorios sem data depois.
    dated = sorted((k for k in pending_keys if candidates[k].get("lastPush")),
                   key=lambda k: str(candidates[k]["lastPush"]), reverse=True)
    undated = sorted(k for k in pending_keys if not candidates[k].get("lastPush"))
    pending_keys = dated + undated
    pending_budget = min(len(pending_keys), max(1, (max_per_run * 2) // 3))
    picked = pending_keys[:pending_budget]
    selected = set(picked)
    if all_keys:
        for i in range(len(all_keys)):
            if len(picked) >= min(max_per_run, len(all_keys)):
                break
            key = all_keys[(start + i) % len(all_keys)]
            if key not in selected:
                picked.append(key)
                selected.add(key)
    next_cursor = (start + min(max_per_run, len(all_keys))) % len(all_keys) if all_keys else 0
    first_checks_scheduled = sum(key not in previous for key in picked)
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
        if own_manifest is not None:
            discovered_valid.append({"host": host, "repo": repo})
        release_list = releases(api, host, repo)
        if own_manifest is None and key not in seed_keys and host == "github":
            # Evidência do próprio autor + release. Sem autoidentificação: fila de revisão.
            readme = get_readme(api, repo)
            entry, category, info = classify_unmanifested(repo, meta, readme, release_list)
        elif own_manifest is None and key not in seed_keys:
            entry, category, info = None, "review", {
                "host": host, "repo": repo, "classification": "review",
                "reason": "Projeto sem manifesto ou verificação explícita", "evidence": f"https://gitlab.com/{repo}"}
        else:
            entry, category, info = make_entry(host, repo,
                {"seed": c if c.get("name") else {}, "manifest": own_manifest, "releases": release_list},
                meta, overrides.get("repositories", {}))
            # Projetos descobertos com manifesto incompleto não ficam ocultos da auditoria.
            if category == "ignored" and key not in seed_keys:
                category = "review"
                info["classification"] = "review"
                info["reason"] = "Manifesto não confirma estado compatível com as duas listas"
        info["discovered_by"] = c.get("discoveredBy", "manifest" if own_manifest else "curated_seed")
        if c.get("matchedQueries"):
            info["matched_queries"] = c["matchedQueries"]
        previous[key] = {"classification": category, "entry": entry, "audit": info}
    playable = [rec["entry"] for key, rec in sorted(previous.items())
                if rec.get("classification") == "playable" and rec.get("entry")]
    developing = [rec["entry"] for key, rec in sorted(previous.items())
                  if rec.get("classification") == "development" and rec.get("entry")]
    playable = dedupe_entries(sorted(playable, key=lambda x: (x["name"].casefold(), x["repository"].casefold())))
    developing = dedupe_entries(sorted(developing, key=lambda x: (x["name"].casefold(), x["repository"].casefold())))
    audit = [rec["audit"] for _, rec in sorted(previous.items())]
    pending = [k for k in all_keys if k not in previous]
    review = [item for item in audit if item.get("classification") == "review"]
    # Atualiza as imagens de TODAS as entradas em todo ciclo, inclusive as
    # armazenadas no snapshot que nao foram revisitadas nesta rodada.
    playable = with_curated_images(playable, overrides)
    developing = with_curated_images(developing, overrides)
    # Base antiga preservada SOMENTE no combinado; suas capas de jogos
    # confiaveis podem ser reaproveitadas, mas avatares pessoais nao.
    legacy = read_json(base / "config/minha-lista-114.json").get("apps", [])
    legacy = with_curated_images(legacy, overrides, legacy=True)
    combined = dedupe_entries([*playable, *developing, *legacy])
    artwork_cache = snapshot.get("artwork", {})
    # Todas as listas recebem as mesmas imagens: as entradas combinadas são as
    # cópias dos registros de playable/developing/legacy usados acima.
    image_stats = populate_author_images(
        api, combined, overrides, artwork_cache,
        max_scans=max(0, min(80, int(config.get("maxArtworkScansPerRun", 30)))), now=now)
    sgdb_cache = snapshot.get("steamgriddb", {})
    sgdb_stats = populate_steamgrid_images(
        sgdb_client, combined, overrides, artwork_cache, sgdb_cache,
        max_lookups=max(0, min(80, int(config.get("maxSteamGridDBLookupsPerRun", 25)))), now=now)
    artwork_lookup = {(e.get("repositorySource", "github"), e.get("repository")): e.get("appIconUrl")
                      for e in combined}
    for item in [*playable, *developing]:
        key = (item.get("repositorySource", "github"), item.get("repository"))
        if key in artwork_lookup and artwork_lookup[key] and not item.get("appIconUrl"):
            item["appIconUrl"] = artwork_lookup[key]
    payloads = {
        "windows-jogaveis.json": {"apps": playable},
        "em-desenvolvimento.json": {"apps": developing},
        "tudo-quiver.json": {"apps": combined},
        "candidatos-revisao.json": {"review": review, "awaiting_first_check": pending,
                                    "notice": "Não importar este relatório no Quiver; candidatos sem confirmação."},
        "relatorio.json": {"summary": {"generator_version": __version__,
                                        "candidate_repositories": len(all_keys),
                                        "first_checks_scheduled": first_checks_scheduled,
                                        "playable_windows": len(playable),
                                       "development": len(developing),
                                       "combined_with_legacy": len(combined),
                                       "tracked": len(audit),
                                       "awaiting_first_check": len(pending),
                                       "checked_this_run": len(picked),
                                       "ignored": sum(i.get("classification") not in ("playable", "development") for i in audit),
                                       "with_images": sum(bool(a.get("appIconUrl")) for a in combined),
                                       "image_discovery": image_stats,
                                       "steamgriddb": sgdb_stats,
                                       "review_candidates": len(review),
                                       "search": search_stats},
                           "projects": audit,
                           "not_yet_checked_repositories": pending}
    }
    # Apenas apos TODAS as consultas obrigatorias completas publicamos dados.
    for filename, body in payloads.items():
        write_json_if_changed(base / "output" / filename, body)
    write_json_if_changed(snapshot_path, {"cursor": next_cursor,
                                          "search_cycle": search_cycle + int(search_attempted),
                                          "manifest_retry_at": manifest_retry_at,
                                          "artwork": artwork_cache,
                                          "steamgriddb": sgdb_cache,
                                          "repositories": dict(sorted(previous.items()))})
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
        sgdb_key = os.getenv("STEAMGRIDDB_API_KEY", "").strip()
        sgdb = SteamGridDBClient(sgdb_key) if sgdb_key else None
        if not sgdb:
            print("AVISO: STEAMGRIDDB_API_KEY ausente; sem fallback de capas.", file=sys.stderr)
        info = regenerate(API(os.getenv("GITHUB_TOKEN", ""), os.getenv("GITLAB_TOKEN", "")), BASE,
                          discovery=not args.no_discovery, sgdb_client=sgdb)
    except (FetchFailure, OSError, ValueError, KeyError, TypeError) as ex:
        print(f"ERRO: publicacao cancelada; arquivos anteriores preservados. {ex}", file=sys.stderr)
        return 1
    print(json.dumps(info, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
