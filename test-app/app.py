import sentry_sdk

DSN = "http://c8ad348058b64f0d9709a4082d93a63f@localhost:8090/4"

sentry_sdk.init(
    dsn=DSN,
    traces_sample_rate=1.0,
)


def divide(a, b):
    return a / b


def fetch_user(user_id):
    users = {"1": "alice", "2": "bob"}
    return users[user_id]  # KeyError if user_id not found


if __name__ == "__main__":
    print(f"Sending test errors to: {DSN}\n")

    # 1. Captured exception with extra context
    try:
        divide(10, 0)
    except ZeroDivisionError as e:
        sentry_sdk.capture_exception(e)
        print("✓ ZeroDivisionError sent")

    # 2. Exception with user and tag context
    with sentry_sdk.new_scope() as scope:
        scope.set_user({"email": "alice@example.com", "id": "1"})
        scope.set_tag("component", "user-service")
        scope.set_extra("request_id", "req-abc-123")
        try:
            fetch_user("999")
        except KeyError as e:
            sentry_sdk.capture_exception(e)
            print("✓ KeyError (with user context) sent")

    # 3. Manual message at warning level
    sentry_sdk.capture_message("Disk usage above 90%", level="warning")
    print("✓ Warning message sent")

    # 4. Unhandled exception (captured automatically by the SDK)
    print("✓ Sending unhandled exception...")
    raise RuntimeError("Unhandled error — connection to downstream service timed out")
