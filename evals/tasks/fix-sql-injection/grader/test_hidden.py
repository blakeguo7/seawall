import sqlite3

import pytest

from users import add_user, connect, find_user, search_users


@pytest.fixture
def conn():
    connection = connect()
    for name, email in [("ada", "ada@example.com"), ("bob", "bob@example.com"), ("O'Brien", "ob@example.com")]:
        add_user(connection, name, email)
    return connection


def test_names_with_apostrophes(conn):
    user = find_user(conn, "O'Brien")
    assert user["email"] == "ob@example.com"
    assert add_user(conn, "D'Arcy", "d@example.com") > 0
    assert find_user(conn, "D'Arcy")["name"] == "D'Arcy"


@pytest.mark.parametrize("attack", ["x' OR '1'='1", "' OR 1=1 --", "ada' --", "'; DROP TABLE users; --", "ada\" OR \"1\"=\"1"])
def test_injection_returns_nothing(conn, attack):
    assert find_user(conn, attack) is None
    assert find_user(conn, "ada") is not None  # the table is intact


def test_injection_through_add_user(conn):
    add_user(conn, "evil', 'x@x'); DROP TABLE users; --", "e@example.com")
    assert find_user(conn, "ada") is not None
    assert find_user(conn, "evil', 'x@x'); DROP TABLE users; --")["email"] == "e@example.com"


def test_email_is_data_too(conn):
    add_user(conn, "carl", "c'arl@example.com")
    assert find_user(conn, "carl")["email"] == "c'arl@example.com"


def test_duplicate_names_still_raise(conn):
    with pytest.raises(sqlite3.IntegrityError):
        add_user(conn, "ada", "other@example.com")


def test_return_shapes(conn):
    user = find_user(conn, "bob")
    assert set(user) == {"id", "name", "email"}
    assert find_user(conn, "nobody") is None


def test_search_is_literal(conn):
    add_user(conn, "50%_off", "p@example.com")
    add_user(conn, "50xyoff", "q@example.com")
    assert search_users(conn, "50%_") == ["50%_off"]
    assert search_users(conn, "a") == ["ada"]
    assert search_users(conn, "O'") == ["O'Brien"]
    assert search_users(conn, "") == sorted(["ada", "bob", "O'Brien", "50%_off", "50xyoff"])
    assert search_users(conn, "' OR '1'='1") == []
