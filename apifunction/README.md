# apifunction

다른 프로젝트에서도 바로 가져다 쓸 수 있도록 API 함수와 키 파일 위치를 한 폴더에 모아둔 패키지입니다.

## 포함된 모듈

- `kosis.py`: KOSIS Open API(Param/statisticsParameterData) 조회
- `ecos.py`: 한국은행 ECOS StatisticItemList/StatisticSearch 조회
- `enara.py`: e-나라지표(index.go.kr) XML 조회
- `world_bank.py`: World Bank WDI 조회
- `imf.py`: IMF DataMapper 조회
- `openfiscal.py`: 열린재정(OpenFiscal) Open API(XML) 조회
- `molit.py`: 국토교통부 통계누리 Open API 조회
- `sigungu_map.js`: 시군구 GIS 코로플레스 렌더러
- `api_keys.py`: 공통 API 키 로더

## API 키 파일 위치

아래 키 파일은 `apifunction` 폴더에 두면 자동으로 읽습니다.

- `ecos_api_key.txt`
- `kosis_api_key.txt`
- `openfiscal_api_key.txt`
- `molit_api_key.txt`

실제 키는 저장소에 커밋하지 말고, 아래 `.example` 파일을 복사해서 사용하세요.

- `ecos_api_key.txt.example`
- `kosis_api_key.txt.example`
- `openfiscal_api_key.txt.example`
- `molit_api_key.txt.example`

또는 환경변수 사용 가능:

- `ECOS_API_KEY`, `ECOS_API_KEY_FILE`
- `KOSIS_API_KEY`, `KOSIS_API_KEY_FILE`
- `OPENFISCAL_API_KEY`, `OPENFISCAL_API_KEY_FILE`
- `MOLIT_API_KEY`, `MOLIT_API_KEY_FILE`

## 빠른 사용 예시 (Python)

```python
from apifunction.ecos import fetch_ecos_statistic_search
from apifunction.kosis import fetch_kosis_table
from apifunction.enara import fetch_enara_table
from apifunction.world_bank import fetch_wb_indicator_panel
from apifunction.imf import fetch_imf_datamapper
from apifunction.openfiscal import fetch_openfiscal_service
from apifunction.molit import (
    fetch_molit_building_permit_stats,
    fetch_molit_public_columns,
    normalize_molit_column_names,
)

df_ecos = fetch_ecos_statistic_search(
    "200Y101", cycle="A", start_time="2000", end_time="2025"
)
df_kosis = fetch_kosis_table("DT_1DA7002S", cycle="A", start_year=2000, end_year=2025)
df_enara = fetch_enara_table(149501, 1495)
df_wb = fetch_wb_indicator_panel(indicator="NE.TRD.GNFS.ZS")
df_imf = fetch_imf_datamapper("NGDP_RPCH", countries=["KOR", "USA"], start_year=2000)
df_openfiscal = fetch_openfiscal_service("OPFI152")
df_molit = fetch_molit_building_permit_stats(start_dt="202001", end_dt="202412")
df_molit_cols = fetch_molit_public_columns(form_id="2202", style_num="838")
df_molit_named = normalize_molit_column_names(df_molit, df_molit_cols)
```

국토교통부 통계누리 Open API는 별도 신청과 인증키 발급이 필요합니다.
공식 안내: [국토교통부 통계누리](https://stat.molit.go.kr/portal/openapi/main.do), [공유서비스 안내](https://stat.molit.go.kr/portal/api/info.do)

건축허가·착공·준공통계 공개 화면은 내부적으로 공개 JSON 엔드포인트를 사용하므로,
`fetch_molit_building_permit_stats()`로 인증키 없이도 동일 표를 가져올 수 있습니다.
숫자 컬럼은 `normalize_molit_column_names()`로 메타 기준 한글 컬럼명으로 바꿀 수 있습니다.

## 시군구 GIS 예시 (JavaScript)

```html
<div id="map"></div>
<script src="https://unpkg.com/leaflet/dist/leaflet.js"></script>
<script src="https://unpkg.com/topojson-client@3"></script>
<script src="./apifunction/sigungu_map.js"></script>
<script>
  renderSigunguMap(document.getElementById("map"), {
    byCode: { "11110": 53.2, "26110": 49.1 },
    itemName: "지표값",
    prdLabel: "2025",
  });
</script>
```

## GovOpenAPI catalog MVP

This repository now also includes a source-level catalog layer for government OpenAPI discovery.
The intended unit is one source statistic or dataset per row, not one endpoint per row.
The generated master catalog currently merges live-collected `ECOS`, `OpenFiscal`, `MOLIT`, `IMF`,
`eNara`, and `World Bank` metadata, and includes a collector for `KOSIS` full-list generation.
The `MOLIT` collector follows the public category tree exposed by `partSttsAjx.do` and then
hydrates each dataset via `statView.do` to recover `form_id`, `style_num`, and date ranges.

- `govopenapi/catalog/schema.py`: normalized master catalog schema
- `govopenapi/catalog/loader.py`: load/save catalog artifacts
- `govopenapi/catalog/search.py`: keyword, source, and tag filtering
- `govopenapi/catalog/ui.py`: Jupyter `ipywidgets` search widget factory
- `govopenapi/auth/credentials.py`: API key loading from args, env vars, and `~/.govapi/credentials.toml`
- `govopenapi/update/source_collectors.py`: live source catalog collectors for OpenFiscal, ECOS, MOLIT, IMF, eNara, World Bank, and KOSIS
- `govopenapi/update/build_catalog.py`: static MVP catalog builder
- `data/source_catalogs/*.jsonl`: per-source raw catalogs
- `data/master_catalog.jsonl`: merged master catalog

Quick example:

```python
from govopenapi import load_catalog, search_catalog

records = load_catalog()
hits = search_catalog(records, text="환율", source="ecos")
```

