#!/bin/bash
echo "=== Portfolio Agent 사전 검증 ==="
PASS=0
FAIL=0

check() {
    if eval "$2" > /dev/null 2>&1; then
        echo "✅ $1"
        PASS=$((PASS+1))
    else
        echo "❌ $1"
        FAIL=$((FAIL+1))
    fi
}

# 1. null bytes 검사
check "dispatcher.py null bytes 없음" "python3 -c \"open('dispatcher.py','rb').read().index(b'\\x00') and exit(1)\" 2>/dev/null; test \$(python3 -c \"print(open('dispatcher.py','rb').read().count(b'\\x00'))\") -eq 0"
check "run_agent.py null bytes 없음" "test \$(python3 -c \"print(open('run_agent.py','rb').read().count(b'\\x00'))\") -eq 0"
check "collector.py null bytes 없음" "test \$(python3 -c \"print(open('collector.py','rb').read().count(b'\\x00'))\") -eq 0"
check "signal_db.py null bytes 없음" "test \$(python3 -c \"print(open('signal_db.py','rb').read().count(b'\\x00'))\") -eq 0"

# 2. 문법 검사
check "dispatcher.py 문법" "python3 -m py_compile dispatcher.py"
check "run_agent.py 문법" "python3 -m py_compile run_agent.py"
check "collector.py 문법" "python3 -m py_compile collector.py"
check "signal_db.py 문법" "python3 -m py_compile signal_db.py"

# 3. 핵심 코드 존재 여부
check "run_agent.py: email 코드 존재" "grep -q 'send_daily_email' run_agent.py"
check "run_agent.py: DONE 존재" "grep -q 'DONE' run_agent.py"
check "run_agent.py: 줄수 200줄 이상" "test \$(wc -l < run_agent.py) -ge 200"
check "dispatcher.py: CSS fix 적용" "grep -q 'replace.*{{' dispatcher.py"
check "dispatcher.py: telegram.send() 사용" "python3 -c \"import re; c=open('dispatcher.py').read(); exit(0 if 'self.telegram._send' not in c else 1)\""
check "collector.py: os import" "grep -q '^import os' collector.py"
check "collector.py: naver API" "grep -q 'openapi.naver.com' collector.py"
check "signal_db.py: weekly_range" "grep -q 'def weekly_range' signal_db.py"

# 4. workflow 파일
check "daily.yml: dashboard.html 커밋" "grep -q 'git add dashboard.html' .github/workflows/daily.yml"
check "daily.yml: NAVER secrets" "grep -q 'NAVER_CLIENT_ID' .github/workflows/daily.yml"

# 5. dashboard.html 구조
check "dashboard.html: 커뮤니케이션 서브탭" "grep -q 'showSubTab' dashboard.html"
check "dashboard.html: DRAFTS 마커" "grep -q 'DRAFTS-AUTO-START' dashboard.html"

echo ""
echo "=== 결과: ✅ $PASS 통과 / ❌ $FAIL 실패 ==="
if [ $FAIL -eq 0 ]; then
    echo ">>> 모든 검증 통과. Run workflow 진행 가능합니다."
else
    echo ">>> 실패 항목 수정 후 다시 검증하세요."
fi
