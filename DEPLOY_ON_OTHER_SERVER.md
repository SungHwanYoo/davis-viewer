# DAVIS 프레임 탐색기를 다른 서버에서 실행하기

이 문서는 현재 DAVIS 프레임 탐색기의 구성과 다른 macOS/Linux 서버로 옮겨 실행하는 방법을 정리한 것입니다. **웹 탐색기** 기준이며, DAVIS 카메라로 새 데이터를 촬영하는 GUI 프로그램은 별도인 `~/davis_event_recorder`입니다.

## 1. 무엇을 옮겨야 하나

현재 탐색기는 세 부분으로 구성됩니다.

| 구성 | 현재 위치 | 역할 |
| --- | --- | --- |
| 웹 서버와 화면 | `~/Documents/Codex/2026-10-04/new-chat/outputs/davis-viewer/` | Python HTTP 서버, 이벤트 렌더러, HTML/CSS/JS |
| 녹화 원본 | `~/davis_recordings/` | APS 이미지, 저장된 이벤트 이미지, `events.bin`, 프레임 메타데이터 |
| 사이트 상태 | 뷰어 폴더의 `data/` | 좋아요·계정 DB와 숨긴 세션 목록 |

다른 서버에서도 같은 데이터를 보려면 **코드뿐 아니라 녹화 원본과 사이트 상태를 함께** 옮겨야 합니다. 녹화 원본은 서버가 읽기만 합니다. 새 녹화 세션을 옮긴 뒤에는 서버를 다시 시작해야 목록에 나타납니다.

필수 파일 구조는 다음과 같습니다. 경로 이름 자체는 바꿔도 되지만 세션 내부의 상대 경로와 파일명은 유지하세요.

```text
~/davis-viewer/
  server.py
  core.py
  web/index.html
  web/app.js
  web/style.css
  web/favicon.svg
  data/likes.sqlite3          # 기존 좋아요·계정을 유지할 때 복사
  data/hidden_sessions.json   # 숨긴 세션을 유지할 때 복사

~/davis_recordings/
  <세션 ID>/
    meta.json
    frames.csv
    events.bin
    frames/frame_000000.png, ...
    events_vis/frame_000000.png, ...
```

세션으로 인식되려면 `meta.json`, `frames.csv`, `events.bin`이 있어야 하고, CSV에 적힌 `frames/` 및 `events_vis/` PNG도 실제로 있어야 합니다. `frames.csv`의 `frame_idx`는 0부터 연속이어야 합니다. 녹화 중인 세션을 복사하면 파일들이 서로 다른 시점의 상태가 될 수 있으므로, **녹화가 끝난 세션**을 복사하세요.

## 2. 현재 서비스가 동작하는 방식

1. `server.py`가 `--recordings` 아래의 세션을 시작 시 스캔합니다. `data/hidden_sessions.json`에 적힌 ID는 목록과 API에서 제외합니다.
2. 브라우저의 `web/app.js`가 세션·프레임 목록을 API로 받고, 왼쪽 APS와 오른쪽 이벤트 이미지를 표시합니다. URL의 `session`, `frame`, `view`, `bin` 또는 `seconds`로 현재 화면을 공유합니다. `bin` 값은 0부터 29까지입니다.
3. 기본 이벤트 이미지는 녹화 당시의 `events_vis` PNG입니다. 누적 시간을 바꾸면 서버가 `events.bin`에서 해당 시간의 이벤트를 읽어 PNG를 새로 만듭니다.
4. **30-bin 보기**는 프레임 기준 시각의 **이전 100 ms**를 30개 반열린 구간 `[시작, 끝)`으로 나눕니다. 기준 시각은 `frame_timestamp_us + exposure_us/2`이며, 옛 세션처럼 노출 시간이 없으면 프레임 타임스탬프를 씁니다. 원본 파일을 `numpy.memmap`으로 열어 시간 인덱스를 이진 탐색하고, 선택 구간만 한 번 메모리에 복사해 bin을 만듭니다. ON은 빨강, OFF는 파랑이고 30개 bin의 밝기 기준은 같습니다. 결과는 서버 메모리에 최근 프레임 12개를 캐시합니다.
5. 좋아요와 계정은 `data/likes.sqlite3`에 저장됩니다. 한 프레임의 좋아요는 0 또는 1개이며 다시 누르면 취소됩니다. 로그인하지 않아도 좋아요를 토글할 수 있고, 로그인한 사용자가 켠 경우 이름을 표시합니다. DB를 복사하면 기존 좋아요와 계정의 암호 해시가 함께 이동합니다.

`events.bin`은 각 이벤트가 `timestamp_us(int64 little-endian)`, `x(uint16)`, `y(uint16)`, `polarity(uint8)`인 **13바이트 packed 레코드**의 연속입니다. `frames.csv`와 `meta.json`을 통해 각 프레임의 시각과 해상도를 읽습니다.

## 3. 다른 서버에 복사

아래는 원본 Mac에서 실행하는 예시입니다. `user@SERVER`를 실제 SSH 접속 주소로 바꾸세요. 서버의 홈 디렉터리에 설치하며, 충분한 디스크 공간을 먼저 확보하세요. `events.bin`이 수 GB인 세션도 있습니다.

```bash
VIEWER='/Users/yoosunghwan/Documents/Codex/2026-10-04/new-chat/outputs/davis-viewer'
SERVER='user@SERVER'

ssh "$SERVER" 'mkdir -p ~/davis-viewer/data ~/davis_recordings'
rsync -a "$VIEWER/core.py" "$VIEWER/server.py" "$VIEWER/web" "$VIEWER/tests" "${SERVER}:~/davis-viewer/"
rsync -a '/Users/yoosunghwan/davis_recordings/' "${SERVER}:~/davis_recordings/"
rsync -a "$VIEWER/data/hidden_sessions.json" "${SERVER}:~/davis-viewer/data/"
```

### 기존 좋아요와 계정 유지

`likes.sqlite3`를 빈 DB로 새로 만들면 기존 좋아요와 계정은 나타나지 않습니다. 원본 사이트가 사용 중일 때는 SQLite의 백업 기능으로 **일관된 사본**을 만든 후 복사하세요. 다음 명령도 원본 Mac에서 실행합니다.

```bash
SNAPSHOT=$(mktemp)
VIEWER_DB="$VIEWER/data/likes.sqlite3" SNAPSHOT="$SNAPSHOT" python3 - <<'PY'
import os
import sqlite3

source = sqlite3.connect(os.environ["VIEWER_DB"])
target = sqlite3.connect(os.environ["SNAPSHOT"])
with target:
    source.backup(target)
target.close()
source.close()
PY

rsync -a "$SNAPSHOT" "${SERVER}:~/davis-viewer/data/likes.sqlite3"
rm "$SNAPSHOT"
ssh "$SERVER" 'chmod 700 ~/davis-viewer/data && chmod 600 ~/davis-viewer/data/likes.sqlite3'
```

이 DB에는 계정 암호 해시와 로그인 세션 정보도 있으므로 공개 저장소에 올리지 마세요. **동시에 두 서버에서 좋아요를 받으면 DB가 자동 동기화되지 않습니다.** 이관 시에는 한 서버를 주 서버로 정하고 새 서버로 전환한 뒤 옛 서버의 공개 접속을 중지해야 좋아요가 갈라지지 않습니다.

## 4. 서버에서 설치·실행

현재 Mac에서는 Python 3.13으로 실행 중입니다. 다른 서버에서도 Python 3.13을 사용하면 환경 차이를 줄일 수 있습니다. **뷰어만 실행할 때** 필요한 외부 Python 패키지는 `numpy`와 `cv2`입니다. GUI 녹화 프로그램용 `dv-processing`과 `PySide6`는 서버 뷰어에 필요하지 않습니다.

```bash
ssh user@SERVER
cd ~/davis-viewer
python3.13 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install numpy opencv-python-headless
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python server.py \
  --recordings "$HOME/davis_recordings" \
  --state-dir "$HOME/davis-viewer/data" \
  --host 127.0.0.1 --port 8765
```

서버에서 다른 터미널을 열고 아래처럼 확인합니다.

```bash
curl -fsS http://127.0.0.1:8765/health
curl -fsS http://127.0.0.1:8765/api/sessions
```

`/health`가 `{"status":"ok","sessions":...}`를 반환하고, `/api/sessions`에 예상한 세션이 있으면 데이터 경로가 맞습니다. 웹 페이지는 서버 자체에서 `http://127.0.0.1:8765`로 열립니다. 현재 Mac의 `start.command`와 `stop.command`는 이 Mac의 절대 경로와 `tmux` 위치를 사용하므로 **다른 서버에 그대로 적용하지 말고 위 명령으로 실행**하세요.

DB를 복사하지 않고 새 계정을 만들려면 서버에서 다음을 **빈 DB일 때만** 실행할 수 있습니다. 기존 DB를 옮겼다면 이 단계는 건너뜁니다. 암호는 실행 중 입력하며 문서나 쉘 명령에 적지 않습니다.

```bash
cd ~/davis-viewer
.venv/bin/python - <<'PY'
from getpass import getpass
from pathlib import Path
from core import LikeStore

store = LikeStore(Path.home() / "davis-viewer/data/likes.sqlite3")
for name in ("석경", "성환", "윤재", "선우"):
    store.create_account(name, getpass(f"{name} 암호: "))
PY
```

## 5. 다른 사람에게 공개

서버 내부의 `127.0.0.1:8765`를 외부에 그대로 열 필요는 없습니다. `cloudflared`를 서버에 설치한 뒤 별도 터미널에서 다음을 실행하면 임시 공개 주소가 출력됩니다.

```bash
cloudflared tunnel --url http://127.0.0.1:8765 --no-autoupdate
```

출력된 `https://…trycloudflare.com` 주소에 기존 화면의 쿼리 문자열을 붙이면 특정 프레임을 공유할 수 있습니다. 예를 들면 `?session=session_20260920_203647&frame=0&view=bins&bin=29`입니다. **Quick Tunnel 주소는 임시 주소이며 재시작할 때 달라집니다.** 예전 `calculator-mac-patent-princeton.trycloudflare.com` 주소를 새 서버에 다시 지정할 수 없습니다. 계속 같은 공개 주소가 필요하면 본인이 관리하는 도메인과 Cloudflare의 **Named Tunnel**을 구성해야 합니다. [Cloudflare Quick Tunnel 문서](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)와 [Named Tunnel 설정 문서](https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/create-local-tunnel/)를 참고하세요.

공개 링크를 아는 사람은 현재 구현에서 세션을 볼 수 있고, 로그인 없이 좋아요도 켜거나 끌 수 있습니다. `events.bin` 원본 다운로드 API는 없지만 APS와 이벤트 이미지가 전달됩니다. 공개 대상과 접근 범위에 맞춰 터널 인증을 추가하거나 별도 인증 설정을 고려하세요.

## 6. 운영·점검

- `data/likes.sqlite3`와 `data/hidden_sessions.json`을 정기적으로 백업하세요. 원본 녹화 폴더와 분리되어 있습니다.
- 숨길 세션을 바꾸거나 녹화 세션을 추가한 뒤에는 뷰어 서버를 재시작하세요. 세션 목록은 시작할 때 읽습니다.
- 사진은 뜨지만 30-bin이 안 뜨면 해당 세션의 `events.bin`이 모두 복사됐는지, 파일 크기가 13의 배수인지, 서버 로그에 렌더 오류가 있는지 확인하세요.
- `curl http://127.0.0.1:8765/health`는 성공하는데 공개 주소가 안 열리면 `cloudflared` 프로세스와 터널 로그를 확인하세요.
- 한 서버에서 좋아요를 저장하고 있는 동안 DB 파일을 단순 복사하는 대신 위의 SQLite 백업 방식을 사용하세요.

이 문서의 명령은 기존 원본 녹화 파일을 수정하지 않습니다. 서버는 녹화 폴더를 읽고, 상태 변경은 `data/likes.sqlite3`에만 기록합니다.
