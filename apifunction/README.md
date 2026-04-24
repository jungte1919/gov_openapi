# apifunction

`apifunction`은 한국 공공/국제 통계 API를 공통 인터페이스로 조회하고,  
`master_table` 기반으로 데이터셋 검색 -> 단건 fetch까지 연결하는 실사용 패키지입니다.

핵심 흐름은 아래 4단계입니다.

1. `build_master_table()`로 source별 카탈로그를 수집해 마스터 테이블 생성
2. `master.csv`로 저장
3. `load_table()`로 빠르게 재로드
4. `search_table()` + `fetch_one()`으로 필요한 데이터만 조회

---

## 1) 포함 모듈

- `master_table.py`: 마스터 테이블 생성/로드/검색/단건 fetch dispatcher
- `kosis.py`: KOSIS Open API (오류코드/재시도/구간 분할 포함)
- `ecos.py`: 한국은행 ECOS 조회
- `enara.py`: e-나라지표 XML 조회
- `world_bank.py`: World Bank indicator 조회
- `imf.py`: IMF DataMapper 조회 + indicators payload 정규화
- `openfiscal.py`: 열린재정 Open API 조회 (`extra_params` 지원)
- `molit.py`: 국토교통부 통계누리 조회
- `oecd_oda.py`: OECD DAC1 SDMX 조회
- `datatollm.py`: DataFrame -> RAG Markdown/JSONL export
- `ai_policy_note.py`: OpenAI Responses API 기반 정책 해설 생성
- `api_keys.py`: 공통 API 키 로더
- `excel_io.py`: Excel 시그니처 기반 안전 로드 유틸
- `sigungu_map.js`: 시군구 GIS 렌더러

---

## 2) API 키 설정

아래 키 파일을 `apifunction` 폴더에 두면 자동으로 읽습니다.

- `ecos_api_key.txt`
- `kosis_api_key.txt`
- `openfiscal_api_key.txt`
- `molit_api_key.txt`

환경변수로도 설정할 수 있습니다.

- `ECOS_API_KEY`, `ECOS_API_KEY_FILE`
- `KOSIS_API_KEY`, `KOSIS_API_KEY_FILE`
- `OPENFISCAL_API_KEY`, `OPENFISCAL_API_KEY_FILE`
- `MOLIT_API_KEY`, `MOLIT_API_KEY_FILE`

`ai_policy_note.py` 사용 시 OpenAI 키도 필요합니다.

- 파일: `openai_api_key.txt` (또는 `opepai_api_key.txt`)
- 환경변수: `OPENAI_API_KEY`, `OPENAI_API_KEY_FILE`

> 보안 권장: 키 파일은 저장소에 커밋하지 마세요.

---

## 3) KOSIS 파일 준비 방법

`build_master_table()`에서 KOSIS 카탈로그를 만들 때 `주제별통계.xls`가 필요합니다.

1. [KOSIS 주제별 통계](https://kosis.kr/statisticsList/statisticsListIndex.do?vwcd=MT_ZTITLE&menuId=M_01_01) 접속
2. `목록 받기` 클릭
3. 우측 상단 `전체 다운로드`로 받은 파일을 `kosis_excel_path`로 사용

---

## 4) 빠른 시작 예시

```python
from master_table import build_master_table, load_table, search_table, fetch_one

# 최초 1회 빌드
master = build_master_table(
    kosis_excel_path="주제별통계.xls",
    include_sources=("kosis", "ecos", "imf", "enara", "molit", "worldbank", "openfiscal"),
    timeout=60,
)
master.to_csv("master.csv", index=False, encoding="utf-8-sig")

# 반복 사용
master = load_table("master.csv")

# 검색
hits = search_table(master, source="worldbank", query="youth", limit=5)
print(hits[["index", "table_id", "table_name"]])

# 단건 fetch
df = fetch_one(master, dataset_id="DT_1DA7002S", source="kosis")
print(df.head())
```

---

## 5) source별 비고

- `KOSIS`: 대량 조회 시 내부적으로 오류코드 기반 재시도/분할 로직이 동작
- `OpenFiscal`: `extra_params`로 연도(`ACNT_YR`) 등 필터 직접 전달 가능
- `MOLIT`: 구간 조회 시 내부적으로 chunk/fallback 처리
- `World Bank`: `countries=None`이면 전체 국가 기준 조회

---

## 6) 참고 문서

- 상세 사용 가이드(마스터 테이블 중심): `usage_examples.html`
- 카탈로그 포맷 설명: `docs/catalog-format.md`

