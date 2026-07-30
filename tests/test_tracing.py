"""Init contract, span parenting across async generators, tenant, redaction path."""
import asyncio

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import obskit
from obskit import tracing


@pytest.fixture()
def exporter():
    """A live tracer wired to an in-memory exporter. No network."""
    tracing.reset_for_tests()
    exp = InMemorySpanExporter()
    res = obskit.init(
        enabled_flag=True,
        service_name="obskit-tests",
        service_namespace="lab",
        deployment_environment="test",
        endpoint="",
        span_processor=SimpleSpanProcessor(exp),
    )
    assert res["status"] == "enabled", res
    yield exp
    tracing.reset_for_tests()


# --- service identity ------------------------------------------------------

@pytest.mark.parametrize("missing", ["service_name", "service_namespace",
                                     "deployment_environment"])
def test_init_requires_service_identity(missing):
    tracing.reset_for_tests()
    kwargs = dict(enabled_flag=True, service_name="a", service_namespace="b",
                  deployment_environment="c", endpoint="")
    kwargs[missing] = None
    with pytest.raises(obskit.ServiceIdentityError) as e:
        obskit.init(**kwargs)
    assert missing in str(e.value)


@pytest.mark.parametrize("blank", ["", "   "])
def test_init_rejects_blank_identity(blank):
    tracing.reset_for_tests()
    with pytest.raises(obskit.ServiceIdentityError):
        obskit.init(enabled_flag=True, service_name=blank, service_namespace="b",
                    deployment_environment="c", endpoint="")


def test_identity_validated_even_when_disabled():
    """A deployment bug must surface whether or not tracing is switched on."""
    tracing.reset_for_tests()
    with pytest.raises(obskit.ServiceIdentityError):
        obskit.init(enabled_flag=False, service_name=None, service_namespace="b",
                    deployment_environment="c", endpoint="")


def test_resource_attributes_are_set(exporter):
    with obskit.span("x"):
        pass
    attrs = exporter.get_finished_spans()[0].resource.attributes
    assert attrs["service.name"] == "obskit-tests"
    assert attrs["service.namespace"] == "lab"
    assert attrs["deployment.environment.name"] == "test"


# --- parenting across an async generator -----------------------------------

def test_span_parenting_survives_async_generator(exporter):
    """The mechanism the module docstring point 2 exists to protect.

    Child spans are opened *inside* an async generator while the parent handle
    was established outside it. Under ambient OTel context alone these orphan;
    they must not here.
    """
    async def gen():
        for i in range(3):
            with obskit.span(f"child-{i}"):
                await asyncio.sleep(0)
            yield i

    async def drive():
        root = obskit.start_root_span("root")
        obskit.adopt_root(root)
        async for _ in gen():
            pass
        obskit.end_root_span(root)

    asyncio.run(drive())

    spans = {s.name: s for s in exporter.get_finished_spans()}
    root = spans["root"]
    assert root.parent is None
    for i in range(3):
        child = spans[f"child-{i}"]
        assert child.parent is not None, f"child-{i} orphaned"
        assert child.parent.span_id == root.context.span_id
        assert child.context.trace_id == root.context.trace_id


def test_nested_spans_parent_to_innermost(exporter):
    with obskit.span("outer"):
        with obskit.span("inner"):
            pass
    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert spans["inner"].parent.span_id == spans["outer"].context.span_id


def test_parent_var_restored_after_block(exporter):
    with obskit.span("a"):
        pass
    with obskit.span("b"):
        pass
    spans = {s.name: s for s in exporter.get_finished_spans()}
    # b must not have become a child of a
    assert spans["b"].parent is None or \
        spans["b"].parent.span_id != spans["a"].context.span_id


# --- tenant ----------------------------------------------------------------

def test_tenant_id_stamped_on_spans(exporter):
    obskit.bind_request(request_id="r1", tenant_id="acme")
    with obskit.span("work"):
        pass
    span = exporter.get_finished_spans()[0]
    assert span.attributes[obskit.App.TENANT_ID] == "acme"
    assert obskit.App.TENANT_ID == "lab.tenant.id"


def test_tenant_flows_into_async_generator(exporter):
    async def drive():
        obskit.bind_request(request_id="r2", tenant_id="globex")
        root = obskit.start_root_span("root")

        async def gen():
            with obskit.span("deep"):
                assert obskit.tenant_id() == "globex"
            yield 1

        async for _ in gen():
            pass
        obskit.end_root_span(root)

    asyncio.run(drive())
    deep = [s for s in exporter.get_finished_spans() if s.name == "deep"][0]
    assert deep.attributes[obskit.App.TENANT_ID] == "globex"


def test_tenant_absent_when_not_bound(exporter):
    tracing.reset_for_tests()
    exp = InMemorySpanExporter()
    obskit.init(enabled_flag=True, service_name="s", service_namespace="n",
                deployment_environment="e", endpoint="",
                span_processor=SimpleSpanProcessor(exp))
    with obskit.span("work"):
        pass
    assert obskit.App.TENANT_ID not in exp.get_finished_spans()[0].attributes


# --- redaction reaches the span --------------------------------------------

def test_redaction_applied_in_attribute_path(exporter):
    with obskit.span("db") as sp:
        sp.set_attribute("lab.db.uri", "mongodb://admin:Changeme001@oci-p:27017/x")
        sp.set_input({"conn": "postgres://u:p4ss@pg:5432/d"})
    a = exporter.get_finished_spans()[0].attributes
    assert "Changeme001" not in a["lab.db.uri"]
    assert "oci-p:27017" in a["lab.db.uri"]
    assert "p4ss" not in a[obskit.Langfuse.OBSERVATION_INPUT]


# --- degradation still behaves ---------------------------------------------

def test_degradation_propagates_to_root(exporter):
    obskit.bind_request(request_id="r3")
    root = obskit.start_root_span("root")
    with obskit.span("dep") as sp:
        sp.set_degraded("backend unreachable", reason="dep_down")
    obskit.end_root_span(root)
    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert spans["dep"].attributes[obskit.App.DEGRADED] is True
    assert spans["root"].attributes[obskit.App.DEGRADED] is True
    assert spans["root"].attributes[obskit.App.DEGRADED_REASON] == "dep_down"


def test_null_span_when_disabled():
    tracing.reset_for_tests()
    obskit.init(enabled_flag=False, service_name="s", service_namespace="n",
                deployment_environment="e", endpoint="")
    with obskit.span("x") as sp:
        assert sp is obskit.NULL_SPAN
        sp.set_attribute("k", "v").set_degraded("m")  # must absorb everything
