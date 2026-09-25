def get_user(user_id: str):
    if user_id == "missing":
        return None  # BUG
    return {"status": 200, "id": user_id}
