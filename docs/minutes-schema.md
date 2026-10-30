# 회의록 스키마 (`<이름>.minutes.json`, version 1)

정의는 `src/rec2md/minutes.py`. 에이전트는 녹취록을 읽고 이 JSON을 쓰고, `rec2md render`는 이것만 읽는다. 렌더러가 다른 필드를 기대하면 스키마를 먼저 고치고(PR) 버전을 올린다.

| 필드 | 형 | 설명 |
|---|---|---|
| `title` | str | 회의명 (필수) |
| `date`, `time`, `place` | str | `2026-10-29`, `10:00-11:30`, 장소 |
| `agenda` | [str] | 안건 |
| `attendees` | [{name, org, role}] | 참석자. 양식은 `org`로 묶어 표시 |
| `author`, `owner` | str | 작성자, 연구자·주관자 |
| `summary` | [str] | 개요 bullet |
| `sections` | [{heading, items}] | 회의내용. `items`는 `{text, children, ts}` 재귀, 3단까지 |
| `decisions` | [str] | 결정사항 |
| `actions` | [{task, owner, due}] | 할 일 |
| `next_meeting` | str | 다음 회의 |
| `attachments` | [str] | 별첨 |
| `source` | {recording, transcript, range} | 근거 녹음과 회의 구간(초) |

양식별 대응 예 (`ji`): `회의일시` ← `date` + `time`, `연구자` ← `owner`, `작성자` ← `author`, `참석자` ← `attendees`(org별 줄), `회의장소` ← `place`, `회의안건` ← `agenda`, `회의내용` ← `sections`(`1. heading` → `▶ item` → `- child`) 뒤에 결정사항·할 일·다음 회의.

예시(합성)
```json
{
 "title": "샘플 프로젝트 착수 회의",
 "date": "2026-01-15", "time": "14:00-15:00", "place": "본관 소회의실",
 "agenda": ["과업 범위 확정"],
 "attendees": [{"name": "홍길동", "org": "가나연구원", "role": "연구위원"}],
 "author": "홍길동", "owner": "홍길동",
 "summary": ["과업 범위를 3개 분야로 확정함"],
 "sections": [{"heading": "과업 범위", "items": [{"text": "분야 구분", "children": [{"text": "교통·환경·관광 3개 분야로 함"}]}]}],
 "decisions": ["3개 분야로 확정"],
 "actions": [{"task": "분야별 자료 수집", "owner": "홍길동", "due": "1/31"}],
 "next_meeting": "2026-02-05 10:00",
 "attachments": [], "source": {"recording": "sample.mp3", "range": [0, 3600]}, "version": 1
}
```
