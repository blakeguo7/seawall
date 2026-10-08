from users import add_user, connect, find_user


def test_round_trip():
    conn = connect()
    add_user(conn, "ada", "ada@example.com")
    assert find_user(conn, "ada")["email"] == "ada@example.com"
