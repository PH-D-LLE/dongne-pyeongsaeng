# 배포하기 — 직접 서버를 올리는 방법

> **이미 올라간 서버를 쓰실 거면 이 문서는 필요 없습니다.**
> Claude 앱 커넥터에 `https://dongne-pyeongsaeng.onrender.com/mcp` 를 등록하면 끝입니다 ([README](README.md) 참고).
>
> 이 문서는 **직접 서버를 운영하거나, 코드를 고쳐 자기 버전을 올릴 때** 필요합니다.

```
GitHub 저장소  →  Render 배포  →  Claude 커넥터 등록
(설계도 보관)     (실제 서버)      (주소 입력)
```

**GitHub만으로는 안 됩니다.** GitHub Pages는 정적 파일만 배달하고, Actions는 상시 서버 용도가 아닙니다. MCP 서버는 누군가 부를 때마다 응답해야 하는 프로그램이라 실행해 줄 곳이 따로 필요합니다.

전체 20~30분, 무료로 시작할 수 있습니다.

---

## ① GitHub에 올리기

### 필요한 파일

| 파일 | 역할 |
|---|---|
| `server.py` | MCP 서버 본체 |
| `sources.py` | 경주·대구 데이터 수집 |
| `requirements.txt` | 필요한 프로그램 목록 |
| `render.yaml` | 배포 설정 |
| `Dockerfile` | 다른 호스팅을 쓸 때만 |
| `.gitignore` | 올리지 않을 파일 목록 |

`selftest.py`, `probe_*.py`는 진단용이라 없어도 배포됩니다.

### 올리는 법

1. https://github.com/new → 이름 입력 → **Public** → **Create repository**
2. **uploading an existing file** 링크 클릭
3. 파일을 창으로 끌어다 놓기
4. **Commit changes**

git 명령어를 몰라도 됩니다. 나중에 파일을 고칠 때도 같은 방법으로 덮어쓰면 됩니다.

---

## ② Render에 배포하기

### 2-1. 가입

https://render.com → GitHub·Google 등으로 로그인

### 2-2. Blueprint 생성

1. **New +** → **Blueprint**
2. 저장소 목록에 안 보이면 아래로 스크롤해 **Public Git Repository** 칸에 저장소 주소를 붙여넣고 **Continue**
   - 이 방법이 GitHub 권한 승인 단계를 건너뛸 수 있어 간단합니다
   - 대신 **자동 배포가 안 됩니다** (아래 주의사항 참고)
3. Blueprint Name 입력 (예: `dongne-pyeongsaeng`)
4. 대구 API 칸(`DAEGU_API_KEY`, `DAEGU_API_URL`)은 **비워 두어도 됩니다.** 나중에 채우면 자동 재배포됩니다
5. **Deploy Blueprint**

3~5분 걸립니다. 로그에 이렇게 뜨면 성공입니다.

```
MCP 서버 시작 — http://0.0.0.0:10000/mcp
StreamableHTTP session manager started
==> Your service is live 🎉
```

### 2-3. 주소 확인

화면 위쪽 주소(`https://이름.onrender.com`)를 **브라우저로 열어 보십시오.** 안내문이 보이면 정상이고, 거기에 커넥터에 넣을 `/mcp` 주소가 표시됩니다.

---

## ③ Claude 앱에 커넥터로 등록

설정 → 커넥터 → 사용자 지정 커넥터 추가

```
이름  : 동네 평생학습
URL   : https://내주소.onrender.com/mcp     ← 끝에 /mcp 필수
```

OAuth 칸 두 개는 비워 둡니다.

---

## ⚠ 자동 배포 주의

**Public Git Repository 방식으로 연결하면 자동 배포가 안 됩니다.** GitHub에 코드를 올려도 Render는 그대로 옛날 버전을 돌립니다.

코드를 고친 뒤에는 Render 대시보드에서 직접 눌러 주십시오.

```
Manual Deploy → Deploy latest commit
```

라이브러리 문제로 실패했다면 **Clear build cache & deploy**를 쓰는 편이 확실합니다.

자동 배포를 원하시면 Render 설정에서 GitHub 계정을 연결하면 됩니다. 권한 승인 단계가 한 번 필요합니다.

---

## 실제로 겪은 문제 두 가지

이 프로젝트를 배포하며 실제로 막혔던 지점입니다. 다른 MCP를 올릴 때도 똑같이 만날 수 있습니다.

### 1. `ModuleNotFoundError: No module named 'mcp.server.fastmcp'`

**원인**: `requirements.txt`에 `mcp>=1.9.0`이라고만 써 두면 최신 **2.0**이 설치됩니다. 2.0에서 모듈 경로가 바뀌어 import가 깨집니다.

**해결**: 버전 상한을 못 박습니다.

```
mcp>=1.9.0,<2.0.0
```

라이브러리가 메이저 버전을 올릴 때 반복되는 사고입니다. 잘 돌던 서버가 갑자기 죽으면 배포 로그에서 이런 줄부터 찾아보십시오.

### 2. `421 Invalid Host header`

**증상**: 배포는 성공하고 브라우저로도 열리는데, Claude 커넥터 연결만 실패합니다.

**원인**: MCP SDK에 DNS 리바인딩 방어 기능이 있어 `Host` 헤더를 검사합니다. 기본값이 localhost 계열만 허용이라, 인터넷에 올리는 순간 자기 도메인조차 막힙니다.

**해결**: `server.py`에서 `TransportSecuritySettings`를 설정합니다. 이 저장소에는 이미 반영돼 있습니다.

```python
# ALLOWED_HOSTS 를 지정하면 → 그 도메인만 허용
# 지정하지 않으면          → 검사를 끈다
```

도메인이 정해져 있다면 Render 환경변수에 넣어 두는 편이 낫습니다.

```
ALLOWED_HOSTS = 내주소.onrender.com
```

이 방어는 원래 '내 PC에서 도는 서버를 악성 웹사이트가 몰래 부르는 것'을 막기 위한 장치라, HTTPS로 공개된 조회 전용 서버에서는 실익이 크지 않습니다.

---

## 무료 요금제의 제약

**15분간 요청이 없으면 서버가 잠듭니다.** 다시 부르면 깨어나는 데 50초쯤 걸립니다.

파일럿·내부 검토용으로는 문제없습니다. 캐시가 10분간 살아 있어서 한 번 깨우면 이어지는 질문들은 빠릅니다.

주민에게 공개하는 단계라면 유료($7/월)로 올리면 항상 깨어 있습니다. 코드는 안 건드려도 됩니다.

### 다른 무료 호스팅

| | 장점 | 단점 |
|---|---|---|
| **Render** | Python 그대로, 설정 간단 | 잠듦 → 첫 응답 50초 |
| **Vercel** | 깨어나는 데 1~2초, 자동 배포 | 요청마다 새로 뜨는 구조라 **캐시가 안 먹습니다** — 매번 기관 서버를 다시 조회 |
| **Cloudflare Workers** | 항상 켜짐, 지연 거의 없음 | JavaScript로 전부 다시 작성해야 함 |
| **Hugging Face Spaces** | Python 그대로 | Render처럼 잠듦 |

**"50초 대기"와 "캐시 없음" 중 무엇이 더 거슬리는가**로 고르시면 됩니다. 기관 서버 부담까지 생각하면 캐시가 되는 쪽이 예의에 맞습니다.

---

## 배포 후 점검

| 확인할 것 | 방법 |
|---|---|
| 서버가 살아 있나 | 브라우저로 `https://주소` 접속 → 안내문 보이면 정상 |
| 툴이 응답하나 | Claude에게 `데이터 소스 점검해줘` (`check_sources`) |
| 데이터가 제대로 오나 | `경주에 지금 신청할 수 있는 강좌 알려줘` |
| 오류 원인 찾기 | Render 대시보드 → **Logs** 탭 |
