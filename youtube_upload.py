#!/usr/bin/env python3
"""Upload a video to YouTube as a Short using the stored OAuth token.

Usage:
    python youtube_upload.py VIDEO.mp4 \
        --title "Did you know..." \
        --description "..." \
        --tags "animals,nature,shorts" \
        --privacy public

Requires token.json (created by youtube_authorize.py) in the same folder.
Re-run youtube_authorize.py if the token has expired (7-day limit while the
OAuth app is in Testing mode).
"""
import argparse
import os
import sys

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]
TOKEN_FILE = "token.json"


def get_service():
    if not os.path.exists(TOKEN_FILE):
        raise SystemExit(
            f"Missing {TOKEN_FILE}. Run: python youtube_authorize.py"
        )
    creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    if not creds.valid:
        raise SystemExit(
            "Stored credentials are invalid/expired. Re-run: "
            "python youtube_authorize.py"
        )
    return build("youtube", "v3", credentials=creds)


def upload(args):
    youtube = get_service()

    vstatus = {
        "privacyStatus": args.privacy,
        "selfDeclaredMadeForKids": False,
    }
    # Scheduled publish: YouTube itself flips the video public at publish_at.
    # The video must be uploaded private until that moment.
    if args.publish_at:
        vstatus["privacyStatus"] = "private"
        vstatus["publishAt"] = args.publish_at
    body = {
        "snippet": {
            "title": args.title,
            "description": args.description,
            "tags": [t.strip() for t in args.tags.split(",") if t.strip()],
            "categoryId": args.category,
        },
        "status": vstatus,
    }

    media = MediaFileUpload(args.video, chunksize=-1, resumable=True,
                            mimetype="video/*")
    request = youtube.videos().insert(
        part="snippet,status", body=body, media_body=media
    )

    print(f"Uploading {args.video} ...")
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"  {int(status.progress() * 100)}%")

    video_id = response["id"]
    print(f"Done. https://youtu.be/{video_id}")
    return video_id


def main():
    p = argparse.ArgumentParser(description="Upload a video to YouTube.")
    p.add_argument("video", help="Path to the .mp4 file")
    p.add_argument("--title", required=True)
    p.add_argument("--description", default="")
    p.add_argument("--tags", default="shorts")
    p.add_argument("--privacy", default="public",
                   choices=["public", "unlisted", "private"])
    p.add_argument("--category", default="22",
                   help="YouTube category id (22 = People & Blogs)")
    p.add_argument("--publish-at", default="",
                   help="RFC3339 UTC time (e.g. 2026-07-16T14:30:00Z) to auto-"
                        "publish; uploads private until then.")
    args = p.parse_args()

    if not os.path.exists(args.video):
        print(f"Video not found: {args.video}", file=sys.stderr)
        sys.exit(1)

    try:
        upload(args)
    except HttpError as e:
        print(f"YouTube API error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
