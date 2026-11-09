"""Render the neofetch-style profile card (dark_mode.svg and light_mode.svg).

Static content comes from profile.json and assets/ascii.txt. GitHub stats are
fetched through the GraphQL API when a token is available (GITHUB_TOKEN or
PROFILE_TOKEN); otherwise the last values saved in cache/stats.json are reused,
so the card can also be rendered offline.

Lines of code are counted from the additions/deletions of every commit authored
by the user on the default branch of each repository. The per-repository result
is cached in cache/loc.json and only recomputed when the branch head changes.
Only the standard library is used.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "profile.json"
ASCII = ROOT / "assets" / "ascii.txt"
STATS_CACHE = ROOT / "cache" / "stats.json"
LOC_CACHE = ROOT / "cache" / "loc.json"
API = "https://api.github.com/graphql"

WIDTH = 58  # characters in the info column

THEMES = {
    "dark": {
        "bg": "#0d1117", "border": "#30363d", "text": "#c9d1d9", "ascii": "#8b949e",
        "key": "#ffa657", "value": "#a5d6ff", "dots": "#484f58", "header": "#7ee787",
        "add": "#3fb950", "del": "#f85149",
    },
    "light": {
        "bg": "#ffffff", "border": "#d0d7de", "text": "#24292f", "ascii": "#57606a",
        "key": "#953800", "value": "#0a3069", "dots": "#afb8c1", "header": "#116329",
        "add": "#1a7f37", "del": "#cf222e",
    },
}
PALETTE = ["#f85149", "#ffa657", "#e3b341", "#3fb950", "#39c5cf", "#58a6ff", "#bc8cff", "#ff7b72"]


# --------------------------------------------------------------------------- dates

def diff_ymd(start, end):
    years = end.year - start.year
    months = end.month - start.month
    days = end.day - start.day
    if days < 0:
        months -= 1
        prev_month_last_day = (date(end.year, end.month, 1) - date.resolution).day
        days += prev_month_last_day
    if months < 0:
        years -= 1
        months += 12
    return years, months, days


def plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def uptime(start, today):
    y, m, d = diff_ymd(start, today)
    return f"{plural(y, 'year')}, {plural(m, 'month')}, {plural(d, 'day')}"


# --------------------------------------------------------------------------- GitHub API

class GitHub:
    def __init__(self, token):
        self.token = token

    def query(self, query, **variables):
        body = json.dumps({"query": query, "variables": variables}).encode()
        req = urllib.request.Request(API, data=body, headers={
            "Authorization": f"bearer {self.token}",
            "Content-Type": "application/json",
            "User-Agent": "profile-card-updater",
        })
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.load(resp)
        if payload.get("errors") and not payload.get("data"):
            raise RuntimeError(payload["errors"])
        return payload["data"]

    def user(self, login):
        return self.query(
            """query($login: String!) {
              user(login: $login) {
                id createdAt
                followers { totalCount }
                repositoriesContributedTo(first: 1, includeUserRepositories: false,
                  contributionTypes: [COMMIT, PULL_REQUEST, REPOSITORY, PULL_REQUEST_REVIEW]) { totalCount }
              }
            }""",
            login=login,
        )["user"]

    def repos(self, login, affiliations):
        repos, cursor = [], None
        while True:
            page = self.query(
                """query($login: String!, $aff: [RepositoryAffiliation], $cursor: String) {
                  user(login: $login) {
                    repositories(first: 100, after: $cursor, ownerAffiliations: $aff) {
                      pageInfo { hasNextPage endCursor }
                      nodes {
                        nameWithOwner isFork stargazerCount
                        owner { login }
                        defaultBranchRef { target { oid } }
                      }
                    }
                  }
                }""",
                login=login, aff=affiliations, cursor=cursor,
            )["user"]["repositories"]
            repos.extend(page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:
                return repos
            cursor = page["pageInfo"]["endCursor"]

    def commits(self, login, since):
        total = 0
        year = since.year
        now = datetime.now(timezone.utc)
        while year <= now.year:
            start = datetime(year, 1, 1, tzinfo=timezone.utc)
            end = min(datetime(year, 12, 31, 23, 59, 59, tzinfo=timezone.utc), now)
            data = self.query(
                """query($login: String!, $from: DateTime!, $to: DateTime!) {
                  user(login: $login) {
                    contributionsCollection(from: $from, to: $to) { totalCommitContributions }
                  }
                }""",
                login=login, **{"from": start.isoformat(), "to": end.isoformat()},
            )
            total += data["user"]["contributionsCollection"]["totalCommitContributions"]
            year += 1
        return total

    def loc(self, name_with_owner, author_id):
        owner, name = name_with_owner.split("/", 1)
        additions = deletions = commits = 0
        cursor = None
        while True:
            data = self.query(
                """query($owner: String!, $name: String!, $id: ID!, $cursor: String) {
                  repository(owner: $owner, name: $name) {
                    defaultBranchRef { target { ... on Commit {
                      history(first: 100, after: $cursor, author: { id: $id }) {
                        totalCount
                        pageInfo { hasNextPage endCursor }
                        nodes { additions deletions }
                      }
                    } } }
                  }
                }""",
                owner=owner, name=name, id=author_id, cursor=cursor,
            )
            ref = (data.get("repository") or {}).get("defaultBranchRef")
            if not ref:
                break
            history = ref["target"]["history"]
            commits = history["totalCount"]
            for node in history["nodes"]:
                additions += node["additions"]
                deletions += node["deletions"]
            if not history["pageInfo"]["hasNextPage"]:
                break
            cursor = history["pageInfo"]["endCursor"]
        return {"additions": additions, "deletions": deletions, "commits": commits}


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def fetch_stats(gh, profile):
    login = profile["login"]
    user = gh.user(login)
    owned = gh.repos(login, ["OWNER"])
    stats = {
        "created_at": user["createdAt"],
        "followers": user["followers"]["totalCount"],
        "contributed": user["repositoriesContributedTo"]["totalCount"],
        "repos": len(owned),
        "stars": sum(r["stargazerCount"] for r in owned),
        "commits": gh.commits(login, datetime.fromisoformat(user["createdAt"].replace("Z", "+00:00"))),
    }

    cache = load_json(LOC_CACHE, {})
    fresh = {}
    for repo in gh.repos(login, profile.get("loc_affiliations", ["OWNER"])):
        if repo["isFork"] or not repo["defaultBranchRef"]:
            continue
        name, head = repo["nameWithOwner"], repo["defaultBranchRef"]["target"]["oid"]
        if cache.get(name, {}).get("head") == head:
            fresh[name] = cache[name]
            continue
        try:
            fresh[name] = {"head": head, **gh.loc(name, user["id"])}
            print(f"loc: counted {name}", file=sys.stderr)
        except (RuntimeError, urllib.error.URLError) as err:
            print(f"loc: skipped {name}: {err}", file=sys.stderr)
            if name in cache:
                fresh[name] = cache[name]
    save_json(LOC_CACHE, fresh)

    stats["additions"] = sum(r["additions"] for r in fresh.values())
    stats["deletions"] = sum(r["deletions"] for r in fresh.values())
    stats["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return stats


# --------------------------------------------------------------------------- rendering

def fmt(n):
    return f"{n:,}"


def leader(key, value, width):
    """Return (key, dots, value) padded with dots so the line is `width` chars long."""
    gap = width - len(key) - len(value) - 3  # ": " + " "
    return key, "." * max(gap, 1), value


def row_kv(key, value, width=WIDTH):
    k, dots, v = leader(key, value, width)
    return [("key", k), ("text", ": "), ("dots", dots), ("text", " "), ("value", v)]


def row_split(left, right, left_width=31):
    sep = [("text", " | ")]
    return row_kv(*left, width=left_width) + sep + row_kv(*right, width=WIDTH - left_width - 3)


def row_title(title):
    if title is None:
        return []
    return [("text", f"- {title} "), ("dots", "-" * (WIDTH - len(title) - 3))]


def build_rows(profile, stats, today):
    created = datetime.fromisoformat(stats["created_at"].replace("Z", "+00:00")).date()
    birth = profile.get("birthdate")
    career = profile.get("career_start")
    values = {
        "uptime": uptime(date.fromisoformat(birth) if birth else created, today),
        "experience": plural(diff_ymd(date.fromisoformat(career), today)[0], "year") if career else "",
    }

    header = profile["header"]
    rows = [[("header", header), ("text", " "), ("dots", "-" * (WIDTH - len(header) - 1))]]
    for index, section in enumerate(profile["sections"]):
        if index or section.get("title"):
            rows.append(row_title(section.get("title")))
        for key, value in section["items"]:
            if key == "Uptime" and not birth:
                key = "Uptime.GitHub"
            rows.append(row_kv(key, value.format(**values)))

    rows.append(row_title("GitHub Stats"))
    rows.append(row_split(("Repos", f"{fmt(stats['repos'])} {{Contributed: {fmt(stats['contributed'])}}}"),
                          ("Stars", fmt(stats["stars"]))))
    rows.append(row_split(("Commits", fmt(stats["commits"])), ("Followers", fmt(stats["followers"]))))
    adds, dels = fmt(stats["additions"]), fmt(stats["deletions"])
    total = fmt(stats["additions"] - stats["deletions"])
    tail = f" ( {adds}++, {dels}-- )"
    k, dots, v = leader("Lines of Code", total + tail, WIDTH)
    rows.append([("key", k), ("text", ": "), ("dots", dots), ("text", " "), ("value", total),
                 ("text", " ( "), ("add", f"{adds}++"), ("text", ", "), ("del", f"{dels}--"), ("text", " )")])
    rows.append([])
    rows.append([("palette", None)])
    return rows


def render(theme, profile, stats, today):
    c = THEMES[theme]
    art = ASCII.read_text().rstrip("\n").split("\n")
    rows = build_rows(profile, stats, today)

    font, line_h, char_w, pad = 16, 20, 9.6, 30
    art_cols = max(len(line) for line in art)
    info_x = pad + art_cols * char_w + 40
    width = int(info_x + WIDTH * char_w + pad)
    height = pad * 2 + max(len(art), len(rows)) * line_h

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="ConsolasFallback,Consolas,\'Courier New\',monospace" '
        f'font-size="{font}px">',
        "<style>",
        f".key{{fill:{c['key']}}}.value{{fill:{c['value']}}}.dots{{fill:{c['dots']}}}"
        f".header{{fill:{c['header']};font-weight:bold}}.add{{fill:{c['add']}}}.del{{fill:{c['del']}}}"
        "text,tspan{white-space:pre}",
        "</style>",
        f'<rect width="{width}" height="{height}" rx="15" fill="{c["bg"]}" stroke="{c["border"]}"/>',
        f'<text x="{pad}" y="{pad}" fill="{c["ascii"]}" xml:space="preserve">',
    ]
    for i, line in enumerate(art):
        out.append(f'<tspan x="{pad}" y="{pad + (i + 1) * line_h - 5}">{escape(line)}</tspan>')
    out.append("</text>")

    out.append(f'<text x="{info_x}" y="{pad}" fill="{c["text"]}" xml:space="preserve">')
    for i, row in enumerate(rows):
        y = pad + (i + 1) * line_h - 5
        if row == [("palette", None)]:
            continue
        spans = "".join(
            escape(text) if kind == "text" else f'<tspan class="{kind}">{escape(text)}</tspan>'
            for kind, text in row
        )
        out.append(f'<tspan x="{info_x}" y="{y}">{spans}</tspan>')
    out.append("</text>")

    palette_row = next(i for i, r in enumerate(rows) if r == [("palette", None)])
    y = pad + palette_row * line_h + 2
    for i, color in enumerate(PALETTE):
        out.append(f'<rect x="{info_x + i * 3 * char_w:.1f}" y="{y}" width="{3 * char_w:.1f}" '
                   f'height="{line_h - 4}" fill="{color}"/>')
    out.append("</svg>")
    return "\n".join(out) + "\n"


def main():
    profile = load_json(PROFILE, None)
    token = os.environ.get("PROFILE_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        stats = fetch_stats(GitHub(token), profile)
        save_json(STATS_CACHE, stats)
    else:
        print("no token: rendering with cache/stats.json", file=sys.stderr)
        stats = load_json(STATS_CACHE, None)
        if stats is None:
            sys.exit("cache/stats.json is missing and no token was provided")

    today = date.fromisoformat(os.environ["TODAY"]) if os.environ.get("TODAY") else date.today()
    for theme in THEMES:
        (ROOT / f"{theme}_mode.svg").write_text(render(theme, profile, stats, today))
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
