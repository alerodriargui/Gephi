import argparse
import csv
import hashlib
import json
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import networkx as nx
import requests
from bs4 import BeautifulSoup


PROJECT_REPOSITORIES = {
    "bitcoin": ("bitcoin/bitcoin",),
    "ethereum": ("ethereum/go-ethereum",),
    "solana": ("solana-labs/solana", "anza-xyz/agave"),
}

OUTPUT_DIR = Path("datos_github_cripto")
CACHE_DIR = OUTPUT_DIR / "cache"
CONTRIBUTORS_CACHE_DIR = CACHE_DIR / "contributors"
FOLLOWING_CACHE_DIR = CACHE_DIR / "following"

CONTRIBUTORS_CSV = OUTPUT_DIR / "contribuyentes_github_cripto.csv"
CONTRIBUTIONS_CSV = OUTPUT_DIR / "contribuciones_repos_github_cripto.csv"
FOLLOWING_CSV = OUTPUT_DIR / "seguimientos_entre_contribuyentes_github_cripto.csv"
STATUS_CSV = OUTPUT_DIR / "estado_recoleccion_github_cripto.csv"
GRAPH_GEXF = OUTPUT_DIR / "grafo_contribuyentes_github_cripto.gexf"
GEPHI_GRAPH_GEXF = OUTPUT_DIR / "grafo_gephi_proyectos_y_seguimientos.gexf"
SUMMARY_JSON = OUTPUT_DIR / "resumen_recoleccion.json"

API_ROOT = "https://api.github.com"
USER_AGENT = "Gephi-crypto-contributor-network/1.0"
REQUEST_TIMEOUT = 30
MAX_RETRIES = 4
THREAD_LOCAL = threading.local()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def write_csv(path, fieldnames, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def api_headers():
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    }
    token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def request_with_retries(session, url, *, params=None, headers=None):
    for attempt in range(MAX_RETRIES):
        try:
            response = session.get(
                url,
                params=params,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException:
            if attempt == MAX_RETRIES - 1:
                raise
            time.sleep((2**attempt) + random.random())
            continue

        if response.status_code not in {429, 500, 502, 503, 504}:
            return response

        if attempt == MAX_RETRIES - 1:
            return response
        retry_after = response.headers.get("Retry-After")
        delay = float(retry_after) if retry_after else (2**attempt) + random.random()
        time.sleep(delay)

    raise RuntimeError(f"No se pudo completar la solicitud: {url}")


def fetch_contributor_page(repository, page, refresh=False):
    owner, name = repository.split("/", 1)
    cache_path = CONTRIBUTORS_CACHE_DIR / owner / name / f"page_{page:04d}.json"
    if cache_path.exists() and not refresh:
        return json.loads(cache_path.read_text(encoding="utf-8"))

    with requests.Session() as session:
        response = request_with_retries(
            session,
            f"{API_ROOT}/repos/{repository}/contributors",
            params={"anon": "1", "per_page": 100, "page": page},
            headers=api_headers(),
        )

    if response.status_code == 403 and response.headers.get("X-RateLimit-Remaining") == "0":
        reset = int(response.headers.get("X-RateLimit-Reset", "0"))
        reset_at = datetime.fromtimestamp(reset, timezone.utc).isoformat()
        raise RuntimeError(
            f"Cuota REST de GitHub agotada; se restablece en {reset_at}. "
            "Vuelve a ejecutar el script para continuar desde la caché."
        )
    response.raise_for_status()

    payload = {
        "repository": repository,
        "page": page,
        "items": response.json(),
        "links": {
            relation: value["url"] for relation, value in response.links.items()
        },
        "fetched_at": utc_now(),
    }
    atomic_write_json(cache_path, payload)
    return payload


def last_page_from_payload(payload):
    last_url = payload.get("links", {}).get("last")
    if not last_url:
        return 1
    values = parse_qs(urlparse(last_url).query).get("page", ["1"])
    return int(values[0])


def collect_repository_contributors(repository, refresh=False):
    first_page = fetch_contributor_page(repository, 1, refresh=refresh)
    pages = [first_page]
    last_page = last_page_from_payload(first_page)
    for page in range(2, last_page + 1):
        pages.append(fetch_contributor_page(repository, page, refresh=refresh))
    return [item for page in pages for item in page["items"]]


def anonymous_key(item):
    identity = "\0".join((item.get("name", ""), item.get("email", "")))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return f"anonymous:{digest}"


def normalize_contributors(raw_by_repository):
    people = {}
    contributions = []

    repository_to_project = {
        repository: project
        for project, repositories in PROJECT_REPOSITORIES.items()
        for repository in repositories
    }

    for repository, items in raw_by_repository.items():
        project = repository_to_project[repository]
        for item in items:
            login = item.get("login")
            if login:
                key = f"github:{login.lower()}"
                account_type = item.get("type") or "User"
                person = people.setdefault(
                    key,
                    {
                        "node_id": key,
                        "login": login,
                        "name": "",
                        "account_type": account_type,
                        "is_bot": account_type == "Bot"
                        or login.lower().endswith("[bot]"),
                        "is_anonymous": False,
                        "github_id": item.get("id") or 0,
                        "profile_url": item.get("html_url")
                        or f"https://github.com/{login}",
                        "avatar_url": item.get("avatar_url") or "",
                        "projects": set(),
                        "repositories": set(),
                        "total_contributions": 0,
                    },
                )
            else:
                key = anonymous_key(item)
                person = people.setdefault(
                    key,
                    {
                        "node_id": key,
                        "login": "",
                        "name": item.get("name") or "Autor anónimo",
                        "account_type": "Anonymous",
                        "is_bot": False,
                        "is_anonymous": True,
                        "github_id": 0,
                        "profile_url": "",
                        "avatar_url": "",
                        "projects": set(),
                        "repositories": set(),
                        "total_contributions": 0,
                    },
                )

            count = int(item.get("contributions") or 0)
            person["projects"].add(project)
            person["repositories"].add(repository)
            person["total_contributions"] += count
            contributions.append(
                {
                    "node_id": key,
                    "login": person["login"],
                    "project": project,
                    "repository": repository,
                    "contributions": count,
                }
            )

    for person in people.values():
        person["projects"] = sorted(person["projects"])
        person["repositories"] = sorted(person["repositories"])
    return people, contributions


def following_cache_path(person):
    stable_id = str(person["github_id"]) if person["github_id"] else person["node_id"]
    return FOLLOWING_CACHE_DIR / f"{stable_id}.json"


def get_thread_session():
    session = getattr(THREAD_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update(
            {
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.8",
                "User-Agent": USER_AGENT,
            }
        )
        THREAD_LOCAL.session = session
    return session


def parse_following_page(html):
    soup = BeautifulSoup(html, "html.parser")
    followed = set()
    for anchor in soup.select('a[data-hovercard-type="user"]'):
        href = anchor.get("href", "")
        if re.fullmatch(r"/[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", href):
            followed.add(href[1:])

    next_url = None
    for anchor in soup.find_all("a", href=True):
        if anchor.get_text(" ", strip=True) == "Next":
            next_url = anchor["href"]
            break
    return followed, next_url


def collect_following_for_person(person, refresh=False):
    cache_path = following_cache_path(person)
    if cache_path.exists() and not refresh:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached.get("complete"):
            return cached

    login = person["login"]
    url = f"https://github.com/{login}?page=1&tab=following"
    followed = set()
    pages = 0
    status = "complete"
    error = ""
    visited = set()
    session = get_thread_session()

    while url and url not in visited:
        visited.add(url)
        response = request_with_retries(session, url)
        pages += 1
        if response.status_code == 404:
            status = "unavailable"
            error = "GitHub no expone la pestaña pública de following"
            break
        if response.status_code != 200:
            status = "error"
            error = f"HTTP {response.status_code}"
            break

        page_followed, next_url = parse_following_page(response.text)
        followed.update(page_followed)
        url = next_url
        if pages >= 500:
            status = "error"
            error = "Límite preventivo de 500 páginas"
            break

    payload = {
        "node_id": person["node_id"],
        "login": login,
        "following": sorted(followed, key=str.lower),
        "following_count_visible": len(followed),
        "pages": pages,
        "status": status,
        "error": error,
        "complete": status in {"complete", "unavailable"},
        "fetched_at": utc_now(),
    }
    atomic_write_json(cache_path, payload)
    return payload


def collect_all_following(people, workers=6, refresh=False):
    eligible = [
        person
        for person in people.values()
        if not person["is_anonymous"] and not person["is_bot"]
    ]
    results = {}
    total = len(eligible)
    completed = 0

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                collect_following_for_person, person, refresh
            ): person["node_id"]
            for person in eligible
        }
        for future in as_completed(futures):
            node_id = futures[future]
            try:
                results[node_id] = future.result()
            except Exception as exc:
                person = people[node_id]
                results[node_id] = {
                    "node_id": node_id,
                    "login": person["login"],
                    "following": [],
                    "following_count_visible": 0,
                    "pages": 0,
                    "status": "error",
                    "error": str(exc),
                    "complete": False,
                    "fetched_at": utc_now(),
                }
            completed += 1
            if completed == total or completed % 100 == 0:
                print(f"Following: {completed}/{total} perfiles procesados", flush=True)

    return results


def build_follow_edges(people, following_results):
    login_to_node = {
        person["login"].lower(): node_id
        for node_id, person in people.items()
        if person["login"]
    }
    edges = set()
    for source, result in following_results.items():
        for followed_login in result["following"]:
            target = login_to_node.get(followed_login.lower())
            if target and target != source:
                edges.add((source, target))
    return sorted(edges)


def write_outputs(people, contributions, following_results, edges):
    people_rows = []
    contribution_totals = {}
    for row in contributions:
        key = (row["node_id"], row["project"])
        contribution_totals[key] = contribution_totals.get(key, 0) + row["contributions"]

    for person in sorted(
        people.values(),
        key=lambda value: (
            value["is_anonymous"],
            (value["login"] or value["name"]).lower(),
        ),
    ):
        following = following_results.get(person["node_id"], {})
        row = {
            "node_id": person["node_id"],
            "login": person["login"],
            "name": person["name"],
            "account_type": person["account_type"],
            "is_bot": person["is_bot"],
            "is_anonymous": person["is_anonymous"],
            "github_id": person["github_id"],
            "profile_url": person["profile_url"],
            "avatar_url": person["avatar_url"],
            "projects": "|".join(person["projects"]),
            "repositories": "|".join(person["repositories"]),
            "repository_count": len(person["repositories"]),
            "total_contributions": person["total_contributions"],
            "bitcoin_contributions": contribution_totals.get(
                (person["node_id"], "bitcoin"), 0
            ),
            "ethereum_contributions": contribution_totals.get(
                (person["node_id"], "ethereum"), 0
            ),
            "solana_contributions": contribution_totals.get(
                (person["node_id"], "solana"), 0
            ),
            "following_status": following.get(
                "status",
                "not_applicable" if person["is_anonymous"] or person["is_bot"] else "missing",
            ),
            "following_count_visible": following.get("following_count_visible", 0),
        }
        people_rows.append(row)

    write_csv(CONTRIBUTORS_CSV, list(people_rows[0]), people_rows)
    write_csv(
        CONTRIBUTIONS_CSV,
        ["node_id", "login", "project", "repository", "contributions"],
        sorted(
            contributions,
            key=lambda row: (
                row["project"],
                row["repository"],
                -row["contributions"],
                row["node_id"],
            ),
        ),
    )

    edge_rows = [
        {
            "source_node_id": source,
            "source_login": people[source]["login"],
            "target_node_id": target,
            "target_login": people[target]["login"],
            "relationship": "follows",
        }
        for source, target in edges
    ]
    write_csv(
        FOLLOWING_CSV,
        [
            "source_node_id",
            "source_login",
            "target_node_id",
            "target_login",
            "relationship",
        ],
        edge_rows,
    )

    status_rows = [
        {
            "node_id": node_id,
            "login": result["login"],
            "status": result["status"],
            "pages": result["pages"],
            "following_count_visible": result["following_count_visible"],
            "error": result["error"],
            "fetched_at": result["fetched_at"],
        }
        for node_id, result in sorted(following_results.items())
    ]
    write_csv(
        STATUS_CSV,
        [
            "node_id",
            "login",
            "status",
            "pages",
            "following_count_visible",
            "error",
            "fetched_at",
        ],
        status_rows,
    )

    graph = nx.DiGraph(
        name="Red de seguimiento entre contribuyentes de Bitcoin, Ethereum y Solana",
        source="GitHub public contributors API and public following pages",
        generated_at=utc_now(),
    )
    for row in people_rows:
        node_id = row["node_id"]
        graph.add_node(
            node_id,
            label=row["login"] or row["name"],
            login=row["login"],
            name=row["name"],
            account_type=row["account_type"],
            is_bot=row["is_bot"],
            is_anonymous=row["is_anonymous"],
            github_id=row["github_id"],
            profile_url=row["profile_url"],
            projects=row["projects"],
            repositories=row["repositories"],
            repository_count=row["repository_count"],
            total_contributions=row["total_contributions"],
            bitcoin_contributions=row["bitcoin_contributions"],
            ethereum_contributions=row["ethereum_contributions"],
            solana_contributions=row["solana_contributions"],
            following_status=row["following_status"],
            following_count_visible=row["following_count_visible"],
        )
    for source, target in edges:
        graph.add_edge(source, target, relationship="follows", weight=1.0)
    nx.write_gexf(graph, GRAPH_GEXF, encoding="utf-8", prettyprint=False)

    project_colors = {
        "bitcoin": {"r": 247, "g": 147, "b": 26, "a": 1.0},
        "ethereum": {"r": 98, "g": 126, "b": 234, "a": 1.0},
        "solana": {"r": 20, "g": 241, "b": 149, "a": 1.0},
    }
    multi_project_color = {"r": 180, "g": 80, "b": 210, "a": 1.0}
    anonymous_color = {"r": 150, "g": 150, "b": 150, "a": 0.75}

    gephi_graph = graph.copy()
    for row in people_rows:
        project_list = row["projects"].split("|") if row["projects"] else []
        if row["is_anonymous"]:
            color = anonymous_color
        elif len(project_list) > 1:
            color = multi_project_color
        else:
            color = project_colors.get(
                project_list[0] if project_list else "",
                anonymous_color,
            )
        gephi_graph.nodes[row["node_id"]]["viz"] = {
            "color": color,
            "size": max(4.0, min(30.0, 4.0 + row["total_contributions"] ** 0.35)),
        }

    for project, color in project_colors.items():
        project_node = f"project:{project}"
        gephi_graph.add_node(
            project_node,
            label=project.capitalize(),
            node_type="project",
            login="",
            name=project.capitalize(),
            account_type="Project",
            is_bot=False,
            is_anonymous=False,
            github_id=0,
            profile_url="",
            projects=project,
            repositories="|".join(PROJECT_REPOSITORIES[project]),
            repository_count=len(PROJECT_REPOSITORIES[project]),
            total_contributions=0,
            bitcoin_contributions=0,
            ethereum_contributions=0,
            solana_contributions=0,
            following_status="not_applicable",
            following_count_visible=0,
            viz={"color": color, "size": 65.0},
        )

    for row in people_rows:
        for project in row["projects"].split("|"):
            if not project:
                continue
            project_contributions = row[f"{project}_contributions"]
            gephi_graph.add_edge(
                row["node_id"],
                f"project:{project}",
                relationship="contributes_to",
                weight=1.0,
                contributions=project_contributions,
            )

    nx.write_gexf(
        gephi_graph,
        GEPHI_GRAPH_GEXF,
        encoding="utf-8",
        prettyprint=False,
    )

    statuses = {}
    for result in following_results.values():
        statuses[result["status"]] = statuses.get(result["status"], 0) + 1
    summary = {
        "generated_at": utc_now(),
        "scope": PROJECT_REPOSITORIES,
        "contributors": len(people),
        "github_accounts": sum(
            not person["is_anonymous"] for person in people.values()
        ),
        "anonymous_contributors": sum(
            person["is_anonymous"] for person in people.values()
        ),
        "bots": sum(person["is_bot"] for person in people.values()),
        "repository_contribution_rows": len(contributions),
        "following_edges_between_contributors": len(edges),
        "project_membership_edges": sum(
            len(person["projects"]) for person in people.values()
        ),
        "following_collection_statuses": statuses,
        "limitations": [
            "Only public GitHub data is collected.",
            "Unavailable or hidden following tabs cannot be reconstructed.",
            "Following edges are restricted to users in the contributor set.",
            "Anonymous commit emails are used only to create a local hash and are never exported.",
        ],
    }
    atomic_write_json(SUMMARY_JSON, summary)
    return summary


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Recoge contribuyentes y relaciones públicas de seguimiento para "
            "repositorios núcleo de Bitcoin, Ethereum y Solana."
        )
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=6,
        help="Número de páginas de perfiles procesadas en paralelo (por defecto: 6).",
    )
    parser.add_argument(
        "--refresh-contributors",
        action="store_true",
        help="Ignora la caché de contribuyentes y vuelve a consultar la API REST.",
    )
    parser.add_argument(
        "--refresh-following",
        action="store_true",
        help="Ignora la caché de following y vuelve a consultar los perfiles.",
    )
    parser.add_argument(
        "--contributors-only",
        action="store_true",
        help="Genera únicamente los datos de contribuyentes, sin consultar following.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if args.workers < 1 or args.workers > 16:
        raise SystemExit("--workers debe estar entre 1 y 16")

    raw_by_repository = {}
    for project, repositories in PROJECT_REPOSITORIES.items():
        for repository in repositories:
            print(f"Contribuyentes: {project} / {repository}", flush=True)
            raw_by_repository[repository] = collect_repository_contributors(
                repository,
                refresh=args.refresh_contributors,
            )

    people, contributions = normalize_contributors(raw_by_repository)
    print(
        f"Contribuyentes únicos: {len(people)} "
        f"({sum(not p['is_anonymous'] for p in people.values())} cuentas GitHub)",
        flush=True,
    )

    if args.contributors_only:
        following_results = {}
    else:
        following_results = collect_all_following(
            people,
            workers=args.workers,
            refresh=args.refresh_following,
        )

    edges = build_follow_edges(people, following_results)
    summary = write_outputs(people, contributions, following_results, edges)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
