from __future__ import annotations

import xml.etree.ElementTree as ET

from spectrace.curated_workflow import load_studiolane_workflows, validate_workflow_quality
from spectrace.workflow_export import export_drawio_bundle, export_mermaid, validate_drawio_xml, validate_mermaid_source


def test_studiolane_original_and_updated_are_verified_horizontal_and_different() -> None:
    pair = load_studiolane_workflows()
    validate_workflow_quality(pair.original, allow_admin_inputs=True)
    validate_workflow_quality(pair.updated, allow_admin_inputs=True)
    assert (len(pair.original.nodes), len(pair.original.edges)) == (32, 38)
    assert (len(pair.updated.nodes), len(pair.updated.edges)) == (39, 48)
    assert {node.node_id for node in pair.original.nodes} < {node.node_id for node in pair.updated.nodes}
    assert "NODE-NO-AUTO-ALLOCATION" in {node.node_id for node in pair.updated.nodes}
    assert not any("Clarification resolved" in (edge.condition or "") for edge in pair.updated.edges)
    for draft, verification in (
        (pair.original, pair.original_verification), (pair.updated, pair.updated_verification),
    ):
        source = export_mermaid(draft, verification, direction="LR", swimlanes=True).content
        assert source.startswith("flowchart LR\n")
        assert source.count("subgraph ") == 4
        validate_mermaid_source(source)


def test_two_page_drawio_bundle_round_trips_all_nodes_edges_and_swimlanes() -> None:
    pair = load_studiolane_workflows()
    bundle = export_drawio_bundle(
        pair.original, pair.original_verification, pair.updated, pair.updated_verification
    )
    root = ET.fromstring(bundle.content)
    validate_drawio_xml(bundle.content, expected_pages=2)
    assert not bundle.content.startswith("%PDF")
    pages = root.findall("diagram")
    assert [page.attrib["name"] for page in pages] == [
        "Original Approved Workflow", "Updated Approved Workflow"
    ]
    expected = (
        (len(pair.original.nodes), len(pair.original.edges)),
        (len(pair.updated.nodes), len(pair.updated.edges)),
    )
    for page, (node_count, edge_count) in zip(pages, expected):
        cells = page.findall(".//mxCell")
        assert len([cell for cell in cells if cell.attrib.get("nodeType")]) == node_count
        assert len([cell for cell in cells if cell.attrib.get("edge") == "1"]) == edge_count
        assert len([cell for cell in cells if "swimlane;" in cell.attrib.get("style", "")]) == 4
