from agentmesh_mcp.compare import extract_records, normalise_provider


def test_extract_records_direct_list():
    rows, total = extract_records([{"id": 1}, {"id": 2}])
    assert rows == [{"id": 1}, {"id": 2}]
    assert total is None


def test_extract_records_wrapped_response_and_count():
    rows, total = extract_records({"agents": [{"id": 7}], "total": 12})
    assert rows == [{"id": 7}]
    assert total == 12


def test_unknown_shape_is_not_misrepresented_as_data():
    rows, total = extract_records({"message": "ok"})
    assert rows == []
    assert total is None


def test_normalise_provider_preserves_source_and_missing_fields():
    listing = {"id": 42, "name": "Code Reviewer", "price": 100}
    item = normalise_provider(listing)
    assert item["agent_id"] == 42
    assert item["name"] == "Code Reviewer"
    assert item["price"] == 100
    assert item["rating"] is None
    assert item["success_rate"] is None
    assert item["capabilities"] is None
    assert item["listing"] == listing
