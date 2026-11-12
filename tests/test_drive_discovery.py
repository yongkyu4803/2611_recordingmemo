"""Drive 목록에서 지원 음성을 고르고 업로드 직후 파일은 미루는지 확인한다."""

from datetime import datetime, timezone
from unittest.mock import Mock

from drive2notion.drive import DriveClient


def test_list_new_audio_filters_formats_and_age_across_pages():
    def file(file_id, name, mime_type, created_time="2020-01-01T00:00:00Z"):
        return {"id": file_id, "name": name, "mimeType": mime_type, "createdTime": created_time}

    client = object.__new__(DriveClient)
    client.service = Mock()
    listing = client.service.files.return_value.list
    listing.return_value.execute.side_effect = [
        {
            "nextPageToken": "page-2",
            "files": [
                file("m4a", "회의.m4a", "audio/mp4"),
                file("mp3", "회의.MP3", "application/octet-stream"),
                file("new", "방금 업로드.mp3", "audio/mpeg", datetime.now(timezone.utc).isoformat()),
            ],
        },
        {"files": [
            file("no-extension", "녹음", "audio/mpeg"),
            file("video", "영상.mp4", "video/mp4"),
            file("text", "메모.txt", "text/plain"),
        ]},
    ]

    files = client.list_new_audio("folder", min_age_minutes=2)

    assert [f.id for f in files] == ["m4a", "mp3", "no-extension"]
    assert [call.kwargs["pageToken"] for call in listing.call_args_list] == [None, "page-2"]
