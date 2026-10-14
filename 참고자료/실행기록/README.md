# 실행 기록 (2026-10-14 기준)

이력 관리 화면에 보이는 비교 실행 기록과 질문 세트 기록입니다. `data/` 폴더는 저장소에 올리지 않게 되어 있고(.gitignore),
실행 기록 하나가 GitHub 파일 크기 한도(100MB)를 넘어 gzip 으로 압축해 둡니다 (305MB → 약 39MB).

| 파일 | 내용 |
|---|---|
| `comparison_runs/<실행 ID>.json.gz` | `data/comparison_runs/<실행 ID>.json` 압축본 17개 — 질문·모델별 답변 원문·CODE·의미 평가 판정·평가 조건 |
| `question_sets.tar.gz` | `data/question_sets/` 전체 — 질문 생성·직접 넣기·고정 세트로 만든 질문 세트와 첨부 파일 |

주요 실행: 58158e3ced6b·3b70b679e48c(질문 세트 300 평가, 소분류 적정 횟수 실측 — 기준 단위·소분류 단위), d552ab00b72e·5b13d77efc78(질문 세트 실행, Opus 평가),
65d50fe3b23d(AIONA 단일28·멀티10/30), eb36f480d499(단일 질문 6개 모델), 56ce8e6826ad(고정 세트 23문항 — 수집 중 서버 재시작으로 중단).
`data/code_eval_runs/` 는 비교 실행이 모델별 평가를 함께 저장한 사본이라 넣지 않았습니다.

## 되살리기

```bash
python -c "import gzip,shutil,pathlib; d=pathlib.Path('data/comparison_runs'); d.mkdir(parents=True,exist_ok=True); [shutil.copyfileobj(gzip.open(f), open(d/f.name[:-3],'wb')) for f in pathlib.Path('참고자료/실행기록/comparison_runs').glob('*.json.gz')]"
```

```bash
python -c "import tarfile; tarfile.open('참고자료/실행기록/question_sets.tar.gz').extractall('data')"
```

서버를 다시 시작하면 결과 비교·이력 관리 화면에 보입니다.
