#!/usr/bin/env python3
"""
Backend QA & Integration Tests for crm_bot
===========================================
Tests FastAPI endpoints and SQLite database integrity.
Requires the server running at http://127.0.0.1:8000 with DEBUG_AUTH=1 env.

Usage:
    DEBUG_AUTH=1 python3 test_crm_api.py
"""

import requests
import sqlite3
import sys
import time

BASE_URL = "http://127.0.0.1:8000"
DB_FILE = "crm_data.db"
HEADERS = {"Authorization": "tma debug"}

# Test lead IDs (synthetic, will be cleaned up)
LEAD_A_CHAT_ID = 999901
LEAD_B_CHAT_ID = 999902
TEST_ACCOUNT_ID = "test_qa_account_001"

PASSED = 0
FAILED = 0
ERRORS = []


def log_result(name: str, success: bool, detail: str = ""):
    global PASSED, FAILED
    if success:
        PASSED += 1
        print(f"  ✅ PASS: {name}")
    else:
        FAILED += 1
        ERRORS.append((name, detail))
        print(f"  ❌ FAIL: {name} — {detail}")


# ===========================================================================
# SETUP: Seed test data directly into the database
# ===========================================================================
def setup_test_data():
    print("\n🔧 SETUP: Seeding test data into crm_data.db ...")
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    # Create a test account
    c.execute(
        "INSERT OR REPLACE INTO accounts (business_connection_id, business_name, user_id, ai_enabled) VALUES (?, ?, ?, ?)",
        (TEST_ACCOUNT_ID, "QA Test Account", 999999, 0),
    )

    # Create two test chats (Lead A and Lead B)
    c.execute(
        "INSERT OR REPLACE INTO chats (chat_id, business_connection_id, lead_name, last_message_time, is_unread, ai_paused) VALUES (?, ?, ?, CURRENT_TIMESTAMP, 0, 0)",
        (LEAD_A_CHAT_ID, TEST_ACCOUNT_ID, "Lead Alpha"),
    )
    c.execute(
        "INSERT OR REPLACE INTO chats (chat_id, business_connection_id, lead_name, last_message_time, is_unread, ai_paused) VALUES (?, ?, ?, CURRENT_TIMESTAMP, 0, 0)",
        (LEAD_B_CHAT_ID, TEST_ACCOUNT_ID, "Lead Beta"),
    )

    # Seed messages for Lead A
    for i, (text, is_out, media_type) in enumerate([
        ("Привет, я Lead Alpha!", False, None),
        ("Здравствуйте! Чем могу помочь?", True, None),
        ("Пришлите фото офиса", False, None),
        ("", True, "photo"),  # outgoing photo
    ]):
        c.execute(
            "INSERT INTO messages (chat_id, text, is_outgoing, media_type, media_url) VALUES (?, ?, ?, ?, ?)",
            (LEAD_A_CHAT_ID, text, is_out, media_type, f"/static/media/test_{i}.jpg" if media_type else None),
        )

    # Seed messages for Lead B
    for text, is_out in [
        ("Добрый день, я Lead Beta", False),
        ("Привет! Рад знакомству", True),
    ]:
        c.execute(
            "INSERT INTO messages (chat_id, text, is_outgoing, media_type, media_url) VALUES (?, ?, ?, ?, ?)",
            (LEAD_B_CHAT_ID, text, is_out, None, None),
        )

    conn.commit()
    conn.close()
    print("  ✅ Test data seeded.\n")


# ===========================================================================
# TEARDOWN: Remove all test data
# ===========================================================================
def teardown_test_data():
    print("\n🧹 TEARDOWN: Cleaning up test data ...")
    for attempt in range(5):
        try:
            conn = sqlite3.connect(DB_FILE, timeout=10)
            c = conn.cursor()
            c.execute("DELETE FROM messages WHERE chat_id IN (?, ?)", (LEAD_A_CHAT_ID, LEAD_B_CHAT_ID))
            c.execute("DELETE FROM chats WHERE chat_id IN (?, ?)", (LEAD_A_CHAT_ID, LEAD_B_CHAT_ID))
            c.execute("DELETE FROM accounts WHERE business_connection_id = ?", (TEST_ACCOUNT_ID,))
            conn.commit()
            conn.close()
            print("  ✅ Test data cleaned.\n")
            return
        except sqlite3.OperationalError as e:
            if "locked" in str(e) and attempt < 4:
                print(f"  ⏳ DB locked, retrying ({attempt+1}/5)...")
                time.sleep(2)
            else:
                raise
    print("  ⚠️ Could not clean test data (DB locked).\n")


# ===========================================================================
# TEST 1: GET /api/chats — list all chats, expect 200 with a list
# ===========================================================================
def test_get_chats():
    print("📋 TEST 1: GET /api/chats")
    try:
        r = requests.get(f"{BASE_URL}/api/chats", headers=HEADERS, timeout=10)
        log_result("Status code is 200", r.status_code == 200, f"Got {r.status_code}: {r.text[:200]}")

        data = r.json()
        log_result("Response is a list", isinstance(data, list), f"Got type {type(data).__name__}")
        log_result("List is non-empty (has existing + test chats)", len(data) > 0, f"Got {len(data)} chats")

        # Verify test chats are present
        chat_ids = [c["chat_id"] for c in data]
        log_result("Lead A (999901) present", LEAD_A_CHAT_ID in chat_ids, f"IDs: {chat_ids[:5]}...")
        log_result("Lead B (999902) present", LEAD_B_CHAT_ID in chat_ids, f"IDs: {chat_ids[:5]}...")

        # Verify expected fields in each chat object
        if data:
            sample = data[0]
            expected_keys = {"chat_id", "business_connection_id", "lead_name", "last_message_time"}
            present_keys = set(sample.keys())
            log_result("Chat object has required keys", expected_keys.issubset(present_keys),
                        f"Missing: {expected_keys - present_keys}")
    except Exception as e:
        log_result("Request succeeded", False, str(e))


# ===========================================================================
# TEST 2: POST /api/accounts/{id}/toggle_ai — toggle AI on/off
# ===========================================================================
def test_toggle_ai():
    print("\n🤖 TEST 2: POST /api/accounts/{id}/toggle_ai")
    url = f"{BASE_URL}/api/accounts/{TEST_ACCOUNT_ID}/toggle_ai"

    # Read initial state
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT ai_enabled FROM accounts WHERE business_connection_id = ?", (TEST_ACCOUNT_ID,))
    initial = c.fetchone()[0]
    conn.close()

    try:
        # Toggle ON
        r1 = requests.post(url, headers=HEADERS, timeout=10)
        log_result("Toggle request returns 200", r1.status_code == 200, f"Got {r1.status_code}: {r1.text[:200]}")

        body1 = r1.json()
        log_result("Response has ai_enabled field", "ai_enabled" in body1, f"Keys: {list(body1.keys())}")

        toggled_value = body1.get("ai_enabled")
        expected_after_toggle = not bool(initial)
        log_result(f"ai_enabled toggled from {bool(initial)} to {expected_after_toggle}",
                    toggled_value == expected_after_toggle,
                    f"Expected {expected_after_toggle}, got {toggled_value}")

        # Toggle back
        r2 = requests.post(url, headers=HEADERS, timeout=10)
        body2 = r2.json()
        log_result(f"Second toggle restores to {bool(initial)}",
                    body2.get("ai_enabled") == bool(initial),
                    f"Expected {bool(initial)}, got {body2.get('ai_enabled')}")

        # Verify DB reflects final state
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT ai_enabled FROM accounts WHERE business_connection_id = ?", (TEST_ACCOUNT_ID,))
        db_val = c.fetchone()[0]
        conn.close()
        log_result("DB reflects toggle state", bool(db_val) == bool(initial),
                    f"DB: {db_val}, expected: {initial}")
    except Exception as e:
        log_result("Toggle request succeeded", False, str(e))


# ===========================================================================
# TEST 3: Context isolation — Lead A and Lead B messages are separated
# ===========================================================================
def test_context_isolation():
    print("\n🔐 TEST 3: Context Isolation (Lead A vs Lead B)")

    try:
        # GET messages for Lead A
        r_a = requests.get(f"{BASE_URL}/api/messages/{LEAD_A_CHAT_ID}", headers=HEADERS, timeout=10)
        log_result("GET messages Lead A returns 200", r_a.status_code == 200, f"Got {r_a.status_code}")
        msgs_a = r_a.json()

        # GET messages for Lead B
        r_b = requests.get(f"{BASE_URL}/api/messages/{LEAD_B_CHAT_ID}", headers=HEADERS, timeout=10)
        log_result("GET messages Lead B returns 200", r_b.status_code == 200, f"Got {r_b.status_code}")
        msgs_b = r_b.json()

        # Lead A should have exactly 4 messages
        log_result("Lead A has 4 messages", len(msgs_a) == 4, f"Got {len(msgs_a)}")

        # Lead B should have exactly 2 messages
        log_result("Lead B has 2 messages", len(msgs_b) == 2, f"Got {len(msgs_b)}")

        # NO message from Lead A should appear in Lead B's history and vice versa
        texts_a = {m["text"] for m in msgs_a if m["text"]}
        texts_b = {m["text"] for m in msgs_b if m["text"]}
        overlap = texts_a & texts_b
        log_result("No message text overlap between Lead A and Lead B", len(overlap) == 0,
                    f"Overlapping texts: {overlap}")

        # Verify Lead A messages contain expected content
        lead_a_texts = [m["text"] for m in msgs_a]
        log_result("Lead A first message correct",
                    "Lead Alpha" in (lead_a_texts[0] if lead_a_texts else ""),
                    f"Got: {lead_a_texts[:1]}")

        # Verify Lead B messages contain expected content
        lead_b_texts = [m["text"] for m in msgs_b]
        log_result("Lead B first message correct",
                    "Lead Beta" in (lead_b_texts[0] if lead_b_texts else ""),
                    f"Got: {lead_b_texts[:1]}")

    except Exception as e:
        log_result("Context isolation test succeeded", False, str(e))


# ===========================================================================
# TEST 4: Direct DB validation — messages table integrity
# ===========================================================================
def test_db_integrity():
    print("\n🗄️  TEST 4: Direct DB Validation (messages table)")

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    try:
        # 4a. Verify is_outgoing flags for Lead A
        c.execute("SELECT text, is_outgoing, media_type, media_url FROM messages WHERE chat_id = ? ORDER BY id ASC",
                  (LEAD_A_CHAT_ID,))
        rows_a = c.fetchall()

        expected_a = [
            ("Привет, я Lead Alpha!", 0, None, None),            # incoming
            ("Здравствуйте! Чем могу помочь?", 1, None, None),   # outgoing
            ("Пришлите фото офиса", 0, None, None),              # incoming
            ("", 1, "photo", "/static/media/test_3.jpg"),         # outgoing photo
        ]

        log_result("Lead A message count in DB = 4", len(rows_a) == 4, f"Got {len(rows_a)}")

        for i, (row, exp) in enumerate(zip(rows_a, expected_a)):
            text, is_out, media_type, media_url = row
            exp_text, exp_out, exp_media, exp_url = exp

            log_result(f"Lead A msg[{i}] is_outgoing={exp_out}",
                        int(is_out) == exp_out,
                        f"Expected {exp_out}, got {is_out}")

            if exp_media:
                log_result(f"Lead A msg[{i}] media_type='{exp_media}'",
                            media_type == exp_media,
                            f"Expected '{exp_media}', got '{media_type}'")
                log_result(f"Lead A msg[{i}] media_url is set",
                            media_url is not None and media_url != "",
                            f"Got '{media_url}'")
            else:
                log_result(f"Lead A msg[{i}] no media_type",
                            media_type is None,
                            f"Expected None, got '{media_type}'")

        # 4b. Verify Lead B messages are isolated in DB  
        c.execute("SELECT chat_id FROM messages WHERE chat_id = ?", (LEAD_B_CHAT_ID,))
        rows_b = c.fetchall()
        log_result("Lead B messages exist in DB", len(rows_b) == 2, f"Got {len(rows_b)}")

        # 4c. Cross-contamination check: no Lead A chat_id in Lead B entries
        c.execute("SELECT COUNT(*) FROM messages WHERE chat_id = ? AND text LIKE '%Alpha%'",
                  (LEAD_B_CHAT_ID,))
        cross = c.fetchone()[0]
        log_result("No Lead A data in Lead B's chat_id", cross == 0, f"Found {cross} cross-contaminated rows")

        # 4d. Verify all messages have valid timestamps
        c.execute("SELECT COUNT(*) FROM messages WHERE chat_id IN (?, ?) AND timestamp IS NULL",
                  (LEAD_A_CHAT_ID, LEAD_B_CHAT_ID))
        null_ts = c.fetchone()[0]
        log_result("All test messages have timestamps", null_ts == 0, f"{null_ts} messages with NULL timestamp")

    except Exception as e:
        log_result("DB integrity test succeeded", False, str(e))
    finally:
        conn.close()


# ===========================================================================
# TEST 5: Auth protection — endpoints reject unauthenticated requests
# ===========================================================================
def test_auth_protection():
    print("\n🔒 TEST 5: Auth Protection (no auth = 401)")
    try:
        r = requests.get(f"{BASE_URL}/api/chats", timeout=10)
        log_result("GET /api/chats without auth returns 401", r.status_code == 401,
                    f"Got {r.status_code}")

        r2 = requests.post(f"{BASE_URL}/api/accounts/{TEST_ACCOUNT_ID}/toggle_ai", timeout=10)
        log_result("POST toggle_ai without auth returns 401", r2.status_code == 401,
                    f"Got {r2.status_code}")
    except Exception as e:
        log_result("Auth test succeeded", False, str(e))


# ===========================================================================
# TEST 6: Edge cases — empty chat, non-existent chat
# ===========================================================================
def test_edge_cases():
    print("\n🧪 TEST 6: Edge Cases")
    try:
        # Messages for non-existent chat
        r = requests.get(f"{BASE_URL}/api/messages/0", headers=HEADERS, timeout=10)
        log_result("GET messages for non-existent chat returns 200 (empty list)",
                    r.status_code == 200 and isinstance(r.json(), list),
                    f"Got {r.status_code}: {r.text[:100]}")

        # Toggle AI for non-existent account — should return 404 (not crash with 500)
        r2 = requests.post(f"{BASE_URL}/api/accounts/nonexistent_id_xyz/toggle_ai", headers=HEADERS, timeout=10)
        log_result("Toggle AI for non-existent account returns 404 (not 500)",
                    r2.status_code == 404,
                    f"Got {r2.status_code}: {r2.text[:200]}")
    except Exception as e:
        log_result("Edge case test succeeded", False, str(e))


# ===========================================================================
# MAIN
# ===========================================================================
if __name__ == "__main__":
    print("=" * 60)
    print("  CRM Bot — Backend QA & Integration Test Suite")
    print("=" * 60)

    # Pre-flight: verify server is up
    try:
        r = requests.get(f"{BASE_URL}/", timeout=5)
        assert r.status_code == 200, f"Server returned {r.status_code}"
        print("✅ Server is reachable at", BASE_URL)
    except Exception as e:
        print(f"❌ Server is NOT reachable at {BASE_URL}: {e}")
        print("   Make sure the server is running with DEBUG_AUTH=1")
        sys.exit(1)

    setup_test_data()

    try:
        test_get_chats()
        test_toggle_ai()
        test_context_isolation()
        test_db_integrity()
        test_auth_protection()
        test_edge_cases()
    finally:
        teardown_test_data()

    print("=" * 60)
    print(f"  RESULTS: {PASSED} passed, {FAILED} failed")
    print("=" * 60)

    if ERRORS:
        print("\n❌ FAILURES:")
        for name, detail in ERRORS:
            print(f"  • {name}: {detail}")
        sys.exit(1)
    else:
        print("\n🎉 All tests passed!")
        sys.exit(0)
