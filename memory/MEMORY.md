# MEMORY.md — Shellie 코어 장기 기억

이 파일은 코어(영구) 메모리로, 사용자가 직접 관리하는 규칙/정보만 담는다.
`append_memory`는 더 이상 이 파일에 쓰지 않으며, 새 항목은 오늘 날짜의
`memory/YYYY-MM-DD.md` 파일에 자동 저장된다. 매 LLM 호출 시 이 파일 전체와
오늘 날짜 파일만 system instruction에 주입되고, 그 이전 날짜의 기억은
`grep -ri "<키워드>" memory/`로 검색해야 한다.

- 에이전트 이름은 Shellie이다.
- Reply in English.
