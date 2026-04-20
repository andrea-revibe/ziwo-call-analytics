import requests
from tqdm import tqdm

from .config import AUDIO_DIR, BASE_URL, PASSWORD, USERNAME, ensure_dirs
from .db import connect, list_by_status, update_status


def _login() -> str:
    r = requests.post(
        f"{BASE_URL}/auth/login",
        json={"username": USERNAME, "password": PASSWORD},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    token = (data.get("content") or {}).get("access_token") or data.get("access_token")
    if not token:
        raise RuntimeError("No access_token in Ziwo login response")
    return token


def download_pending(limit: int | None = None) -> tuple[int, int]:
    ensure_dirs()
    token = _login()
    done = failed = 0

    with connect() as conn:
        rows = list_by_status(conn, "pending", limit=limit)
        if not rows:
            return 0, 0

        for row in tqdm(rows, desc="downloading", unit="mp3"):
            call_id = row["id"]
            rec_file = row["recording_file"]
            if not rec_file:
                update_status(
                    conn, call_id, "failed", error_message="no recording_file"
                )
                failed += 1
                conn.commit()
                continue

            rec_id = rec_file.split(".")[0]
            url = f"{BASE_URL}/callHistory/{rec_id}/recording?access_token={token}"
            out_path = AUDIO_DIR / f"{call_id}.mp3"

            try:
                resp = requests.get(url, timeout=120, stream=True)
                resp.raise_for_status()
                with open(out_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=64 * 1024):
                        if chunk:
                            f.write(chunk)
                update_status(
                    conn,
                    call_id,
                    "downloaded",
                    audio_path=str(out_path),
                    error_message=None,
                )
                done += 1
            except Exception as e:
                if out_path.exists():
                    out_path.unlink(missing_ok=True)
                update_status(
                    conn, call_id, "failed", error_message=f"download: {e}"
                )
                failed += 1
            conn.commit()

    return done, failed
