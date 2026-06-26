RELEASE_TYPE_LABELS: dict[int, str] = {
    1: "Premiere",
    2: "Theatrical (limited)",
    3: "Theatrical",
    4: "Digital",
    5: "Physical",
    6: "TV",
}


def release_type_label(t: int) -> str:
    return RELEASE_TYPE_LABELS.get(t, f"Unknown ({t})")
