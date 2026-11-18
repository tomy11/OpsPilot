from aiw.agent.policy import Decision, authorize_tool, can_decide
from aiw.agent.tools import TOOLS
from aiw.core.auth import Role, User

staff = User(id="s1", role=Role.STAFF, department="ops")
manager = User(id="m1", role=Role.MANAGER, department="ops")
other_manager = User(id="m2", role=Role.MANAGER, department="finance")
admin = User(id="a1", role=Role.ADMIN, department="it")


def test_read_tools_allowed_for_staff():
    assert authorize_tool(staff, TOOLS["get_order"]).decision is Decision.ALLOW


def test_write_tools_always_need_approval_even_for_admin():
    assert authorize_tool(staff, TOOLS["create_stock_transfer"]).decision is Decision.NEEDS_APPROVAL
    assert authorize_tool(admin, TOOLS["adjust_inventory"]).decision is Decision.NEEDS_APPROVAL


def test_admin_only_tool_denied_for_staff_and_manager():
    assert authorize_tool(staff, TOOLS["adjust_inventory"]).decision is Decision.DENY
    assert authorize_tool(manager, TOOLS["adjust_inventory"]).decision is Decision.DENY


def test_approver_rules():
    assert can_decide(staff, requested_by="x", department="ops").decision is Decision.DENY
    assert can_decide(manager, requested_by="m1", department="ops").decision is Decision.DENY
    assert can_decide(other_manager, requested_by="s1", department="ops").decision is Decision.DENY
    assert can_decide(manager, requested_by="s1", department="ops").decision is Decision.ALLOW
    assert can_decide(admin, requested_by="s1", department="ops").decision is Decision.ALLOW


def test_tool_schemas_are_strict():
    for spec in TOOLS.values():
        schema = spec.anthropic_schema()
        assert schema["strict"] is True
        assert schema["input_schema"]["additionalProperties"] is False
        assert set(schema["input_schema"]["required"]) == set(schema["input_schema"]["properties"])
