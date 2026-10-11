"""Two authored Remy-shaped pages with offline, hash-verified provenance."""

import hashlib
import json
from pathlib import Path


def write_remy_catalog(directory):
    root = Path(directory)
    (root / "raw").mkdir(parents=True, exist_ok=True)
    (root / "remy-research").mkdir(exist_ok=True)
    specs = [
        (
            2,
            "Synthetic Standard",
            "Test Artist",
            "standard",
            "PiNK PLUS",
            "2016-06-30",
            "201",
            "13",
        ),
        (3, "Synthetic DX", "Test Artist", "dx", "FESTiVAL", "2022-09-15", "202", "14"),
        (
            4,
            "Synthetic Mismatch",
            "Different Artist",
            "standard",
            "FESTiVAL",
            "2022-09-15",
            "202",
            "12",
        ),
    ]
    songs = [
        {
            "id": 1,
            "title": "Prior Evidence",
            "artist": "Test Artist",
            "difficulties": {"standard": [{"difficulty": 4}], "dx": []},
        }
    ]
    for sid, title, artist, kind, *_ in specs:
        songs.append(
            {
                "id": sid,
                "title": title,
                "artist": "Catalog Artist" if sid == 4 else artist,
                "difficulties": {"standard": [], "dx": [], kind: [{"difficulty": 4}]},
            }
        )
    document = {"songs": songs}
    prior = {
        "chart_key": "1:standard:4",
        "title": "Prior Evidence",
        "type": "standard",
        "verified": True,
        "jp_release_version": "ORANGE PLUS",
        "jp_release_date": "2015-03-19",
        "source_url": "https://silentblue.remywiki.com/Prior_Evidence",
        "source_provider": "remywiki",
        "evidence": "Synthetic prior verified evidence",
    }
    records = []
    manifest = []
    for version, identity, label in [
        ("PiNK PLUS", "maimai:PiNK_PLUS", "maimai PiNK PLUS"),
        ("FESTiVAL", "maimai_DX:FESTiVAL", "maimai でらっくす FESTiVAL"),
    ]:
        rows = [spec for spec in specs if spec[4] == version]
        date, revision = rows[0][5:7]
        body = '<tr><th colspan="2">Song</th><th>Artist</th><th>Basic</th><th>Advanced</th><th>Expert</th><th>Master</th><th>Re:Master</th></tr>'
        body += f'<tr><th colspan="8" id="{date.replace("-", "")}">Launch</th></tr>'
        for _, title, artist, kind, *_, level in rows:
            body += f"<tr><td>{title}</td><td>{'DX' if kind == 'dx' else 'STD'}</td><td>{artist}</td><td>-</td><td>-</td><td>-</td><td>-</td><td>{level}</td></tr>"
        html = f'''<!DOCTYPE html><title>{label} - SilentBlue.RemyWiki</title>
<script>RLCONF={{"wgPageName":"{identity}","wgRevisionId":{revision}}};</script>
<h1>{label}</h1><h2>New Songs / DX Charts</h2><table>{body}</table>'''
        evidence_file = f"remy-research/{revision}.html"
        data = html.encode("utf-8")
        (root / evidence_file).write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        url = f"https://silentblue.remywiki.com/{identity}"
        manifest.append(
            {
                "source_url": url,
                "file": evidence_file,
                "sha256": digest,
                "retrieval_method": "internet_archive_snapshot",
            }
        )
        for sid, title, artist, kind, _, date, revision, level in rows:
            records.append(
                {
                    "chart_key": f"{sid}:{kind}:4",
                    "title": title,
                    "artist": artist,
                    "type": kind,
                    "jp_release_version": version,
                    "jp_release_date": date,
                    "source_url": url,
                    "source_revision": revision,
                    "verified": True,
                    "source_provider": "remywiki",
                    "evidence": "Synthetic dated table row",
                    "evidence_file": evidence_file,
                    "evidence_sha256": digest,
                    "evidence_row": [
                        title,
                        "DX" if kind == "dx" else "STD",
                        artist,
                        "-",
                        "-",
                        "-",
                        "-",
                        level,
                    ],
                }
            )
    for path, value in [
        ("raw/lxns_songs.json", document),
        ("remy_release_overrides.json", {"overrides": [prior]}),
        ("remy-research/evidence_manifest.json", {"pages": manifest}),
    ]:
        (root / path).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return document, [prior], records
