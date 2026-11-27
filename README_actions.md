# GitHub Actions 로 매일 자동 수집 (설정 순서)

기존 `rci-collector`(README.md 참고)를 그대로 쓰되, 내 PC 대신 GitHub 서버가 매일 돌립니다.
결과(`inbox.json`)는 GitHub Pages 주소로 공개되고, 캘린더 「수집함」이 그 주소를 읽어 후보를 자동으로 가져옵니다. 승인은 사람이 합니다.

## 1. 저장소 만들기 (1회)
1. GitHub 에서 새 저장소를 만들고(예: `rci-collector`) 이 폴더의 **내용 전체**를 루트에 올립니다(`.github`, `.gitignore` 포함).
2. **Settings → Pages → Source: GitHub Actions**
3. **Settings → Actions → General → Workflow permissions: Read and write permissions**
4. `pool.json` 은 현재 캘린더 데이터로 만들어 두었습니다. Pool 을 바꾸면 캘린더에서 다시 내보내 덮어쓰세요.

> 공개(Public) 저장소면 `pool.json`·수집 결과가 공개됩니다(경쟁사 공개 일정과 기업명뿐, 자사 일정·내부 자료는 없음). 비공개로 두면 Pages 사용 가능 여부는 GitHub 요금제에 따라 다릅니다.

## 2. 먼저 점검 (권장)
**Actions → RCI Collector → Run workflow → mode: probe** → 끝나면 실행 화면 아래 Artifacts 의 `probe-result` 를 받습니다.
GitHub 서버에서 각 출처(삼성·SK하이닉스 뉴스룸 등)가 접속되는지 확인하는 단계입니다. 회사 PC 에서 되던 사이트가 해외 서버 접속을 막는 경우가 있어, 막히는 출처는 회사 PC 실행(run.bat)으로 보완해야 합니다.

## 3. 첫 수집
**Run workflow → mode: collect**. 끝나면 `https://<아이디>.github.io/<저장소>/inbox.json` 이 열립니다. 이후 하루 4번(KST 05:40 · 11:40 · 17:40 · 23:40) 자동 실행됩니다. 언론사 RSS는 최근 하루치 기사만 담고 있어서 자주 돌려야 놓치지 않습니다.

## 4. 캘린더 연결
캘린더(admin) → 데이터 관리 → **수집함** → 「자동 수집 주소」에 위 inbox.json 주소를 넣고 **저장하고 지금 가져오기**.
그다음부터는 admin 에서 캘린더를 열 때마다 더 새 결과가 있으면 자동으로 후보가 들어옵니다.

## 5. 알아둘 점
- 수집 이력(`data/collector.sqlite`)은 매번 저장소에 커밋되어 이어집니다(중복·변경 감지용).
- 출처를 늘리려면 `config.json` 의 `sources` 에 추가(README.md 5장) 후 push → 다음 실행부터 반영됩니다.
- 종료코드 1(일부 출처 실패)은 경고로 두고 계속 진행합니다. 수집함의 '출처별 수집 상태'와 Actions 로그로 확인하세요.
- 에브리타임·자소설 등 로그인 필요한 출처는 수집하지 않습니다(기존 원칙 유지).
- 공개 저장소라면 inbox.json의 '원문 문장'(기사 한 문장, 최대 200자)도 공개됩니다. 신경 쓰이면 저장소를 비공개로 두세요.

## 6. 네이버 뉴스 검색 연결 (선택, 권장)
언론사 RSS는 **지금부터 나오는 기사**만 잡습니다. 네이버 검색 API를 연결하면 Pool 기업마다 최근 기사를 검색해서 지난 기사와 RSS가 없는 매체까지 찾습니다. 무료이며 하루 25,000회까지 쓸 수 있습니다(이 수집기는 1회 실행에 약 70회 사용).
1. https://developers.naver.com → 로그인 → **Application → 애플리케이션 등록**. 사용 API: **검색**, 환경: **WEB 설정**(URL은 GitHub Pages 주소 등 아무 주소).
2. 등록하면 나오는 **Client ID**와 **Client Secret**을 복사합니다.
3. GitHub 저장소 → **Settings → Secrets and variables → Actions → New repository secret** 으로 두 개를 만듭니다.
   - 이름 `NAVER_CLIENT_ID` → 값: Client ID
   - 이름 `NAVER_CLIENT_SECRET` → 값: Client Secret
4. 다음 실행부터 「네이버 뉴스 검색(Pool 기업별)」 출처가 동작합니다. 키가 없으면 이 출처만 조용히 건너뜁니다.

회사 PC(run.bat)에서 쓸 때는 명령 프롬프트에서 `set NAVER_CLIENT_ID=...`, `set NAVER_CLIENT_SECRET=...` 를 먼저 입력하고 실행합니다. 키는 config.json 등 파일에 적지 마세요(저장소에 올라가면 공개됩니다).
