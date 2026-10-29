from pathlib import Path

import httpx

from scripts.benchmark_download import loopback_source


def test_loopback_control_serves_only_pinned_bytes_with_valid_ranges(tmp_path: Path) -> None:
    payload = b"0123456789" * 4096
    directory = tmp_path / "tls"
    with loopback_source({"/file.bin": (payload, '"same"')}, directory) as (url, cert):
        import ssl

        with httpx.Client(verify=ssl.create_default_context(cafile=str(cert))) as client:
            response = client.head(url + "/file.bin")
            assert response.status_code == 200
            assert response.headers["Content-Length"] == str(len(payload))
            assert response.content == b""
            response = client.get(url + "/file.bin", headers={"Range": "bytes=9-27"})
            assert response.status_code == 206
            assert response.headers["Content-Range"] == f"bytes 9-27/{len(payload)}"
            assert response.content == payload[9:28]
            response = client.get(
                url + "/file.bin", headers={"Range": "bytes=9-", "If-Range": '"changed"'}
            )
            assert response.status_code == 200
            assert response.content == payload
            assert (
                client.get(url + "/file.bin", headers={"Range": "bytes=999999-"}).status_code == 416
            )
            for path in ("/key.pem", "/../key.pem", "/file.bin?anything"):
                assert client.get(url + path).status_code == 404
    assert directory.stat().st_mode & 0o777 == 0o700
    assert (directory / "key.pem").stat().st_mode & 0o777 == 0o600
