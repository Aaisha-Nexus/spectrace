"""Deterministic Mermaid and editable Draw.io exports for verified workflows."""

from __future__ import annotations

import hashlib
import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from spectrace.workflow import (
    WorkflowChangeType,
    WorkflowDraft,
    WorkflowNode,
    WorkflowNodeType,
    WorkflowVerificationResult,
    require_verified_workflow,
)


@dataclass(frozen=True)
class WorkflowExport:
    content: str
    sha256: str


DRAWIO_MIME_TYPE = "application/vnd.jgraph.mxfile"


def validate_mermaid_source(source: str) -> None:
    """Validate the deterministic subset emitted by this module before download."""

    lines = source.splitlines()
    if not lines or lines[0] not in {"flowchart TD", "flowchart LR"}:
        raise ValueError("invalid Mermaid flowchart header")
    declared: set[str] = set()
    referenced: set[str] = set()
    node_pattern = re.compile(r"^\s+(n_[A-Za-z0-9_]+)(?:\(\[|\{|\[\[|>|\[)")
    edge_pattern = re.compile(
        r"^\s+(n_[A-Za-z0-9_]+)\s+-->(?:\|[^|]*\|)?\s+(n_[A-Za-z0-9_]+)$"
    )
    for line in lines[1:]:
        node_match = node_pattern.match(line)
        if node_match:
            declared.add(node_match.group(1))
            if line.count('"') % 2:
                raise ValueError("unbalanced quoted Mermaid node label")
            continue
        edge_match = edge_pattern.match(line)
        if edge_match:
            referenced.update(edge_match.groups())
    if not declared:
        raise ValueError("Mermaid source declares no workflow nodes")
    if referenced - declared:
        raise ValueError("Mermaid edge references an undeclared node")


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _mermaid_text(value: str) -> str:
    value = value.replace("\r", " ").replace("\n", " ")
    value = re.sub(r"\s+", " ", value).strip()
    return (
        html.escape(value, quote=True)
        .replace("[", "&#91;")
        .replace("]", "&#93;")
        .replace(":", "&#58;")
        .replace("|", "&#124;")
    )


def _mermaid_id(value: str) -> str:
    """Map stable domain IDs to Mermaid-safe alphanumeric identifiers."""

    safe = re.sub(r"[^A-Za-z0-9_]", "_", value)
    return f"n_{safe}" if not safe.startswith("n_") else safe


def _mermaid_node(node: WorkflowNode, label: str) -> str:
    node_id = _mermaid_id(node.node_id)
    safe = _mermaid_text(label)
    if node.node_type in {WorkflowNodeType.START, WorkflowNodeType.END}:
        return f'{node_id}(["{safe}"])'
    if node.node_type == WorkflowNodeType.DECISION:
        return f'{node_id}{{"{safe}"}}'
    if node.node_type == WorkflowNodeType.ERROR:
        return f'{node_id}[["{safe}"]]'
    if node.node_type == WorkflowNodeType.CLARIFICATION:
        return f'{node_id}>"{safe}"]'
    return f'{node_id}["{safe}"]'


def export_mermaid(
    draft: WorkflowDraft,
    verification: WorkflowVerificationResult,
    *,
    direction: str = "TD",
    swimlanes: bool = False,
    highlight_changes: bool = True,
    reference_draft: WorkflowDraft | None = None,
) -> WorkflowExport:
    require_verified_workflow(draft, verification)
    if direction not in {"TD", "LR"}:
        raise ValueError("Mermaid direction must be TD or LR")
    actor_by_id = {actor.actor_id: actor.label for actor in draft.actors}
    lines = [f"flowchart {direction}"]
    if swimlanes:
        for actor in draft.actors:
            actor_id = _mermaid_id(f"LANE-{actor.actor_id}")
            lines.append(f'    subgraph {actor_id}["{_mermaid_text(actor.label)}"]')
            lines.append(f"        direction {direction}")
            for node in draft.nodes:
                if node.actor_id == actor.actor_id:
                    lines.append(f"        {_mermaid_node(node, node.label)}")
            lines.append("    end")
    else:
        for node in draft.nodes:
            label = f"{node.label} · {actor_by_id[node.actor_id]}"
            lines.append(f"    {_mermaid_node(node, label)}")
    for edge in draft.edges:
        condition = f"|{_mermaid_text(edge.condition)}|" if edge.condition else ""
        lines.append(
            f"    {_mermaid_id(edge.source_id)} -->{condition} {_mermaid_id(edge.target_id)}"
        )
    lines.extend(
        [
            "    classDef added fill:#073b32,color:#ecfdf5,stroke:#34d399,stroke-width:3px",
            "    classDef modified fill:#3b2b07,color:#fffbeb,stroke:#fbbf24,stroke-width:3px",
            "    classDef clarification fill:#3b2b07,color:#fffbeb,stroke:#fbbf24,stroke-width:3px,stroke-dasharray:5 3",
            "    classDef error fill:#3a101a,color:#fff1f2,stroke:#fb7185,stroke-width:2px",
            "    classDef system fill:#102a43,color:#f0f9ff,stroke:#38bdf8",
        ]
    )
    classes = {
        "added": [_mermaid_id(node.node_id) for node in draft.nodes if highlight_changes and node.change_type == WorkflowChangeType.ADDED],
        "modified": [_mermaid_id(node.node_id) for node in draft.nodes if highlight_changes and node.change_type == WorkflowChangeType.MODIFIED],
        "clarification": [_mermaid_id(node.node_id) for node in draft.nodes if node.node_type == WorkflowNodeType.CLARIFICATION],
        "error": [_mermaid_id(node.node_id) for node in draft.nodes if node.node_type == WorkflowNodeType.ERROR],
        "system": [_mermaid_id(node.node_id) for node in draft.nodes if node.node_type == WorkflowNodeType.SYSTEM],
    }
    for class_name, node_ids in classes.items():
        if node_ids:
            lines.append(f"    class {','.join(node_ids)} {class_name}")
    if highlight_changes and reference_draft is not None:
        reference_edges = {edge.edge_id for edge in reference_draft.edges}
        changed_indexes = [
            str(index) for index, edge in enumerate(draft.edges)
            if edge.edge_id not in reference_edges
        ]
        if changed_indexes:
            lines.append(
                f"    linkStyle {','.join(changed_indexes)} stroke:#5C8768,stroke-width:3px"
            )
    lines.append(f"    %% draft_sha256: {draft.draft_hash}")
    content = "\n".join(lines) + "\n"
    validate_mermaid_source(content)
    return WorkflowExport(content=content, sha256=_hash(content))


_ACTOR_COLORS = ("#E8F1FF", "#EAF8F0", "#FFF3DF", "#F5EBFF", "#FFECEF")


def _drawio_style(node: WorkflowNode, fill: str) -> str:
    base = f"whiteSpace=wrap;html=1;fillColor={fill};strokeColor=#52657A;fontColor=#152536;"
    if node.node_type in {WorkflowNodeType.START, WorkflowNodeType.END}:
        base += "ellipse;"
    elif node.node_type == WorkflowNodeType.DECISION:
        base += "rhombus;"
    elif node.node_type == WorkflowNodeType.ERROR:
        base += "shape=process;fillColor=#FFE4E6;strokeColor=#BE123C;"
    elif node.node_type == WorkflowNodeType.CLARIFICATION:
        base += "rounded=1;dashed=1;fillColor=#F4E8FF;strokeColor=#7B2CBF;"
    else:
        base += "rounded=1;"
    if node.change_type == WorkflowChangeType.ADDED:
        base += "strokeColor=#198754;strokeWidth=3;"
    elif node.change_type == WorkflowChangeType.MODIFIED:
        base += "strokeColor=#C47F00;strokeWidth=3;"
    return base


def export_drawio(
    draft: WorkflowDraft,
    verification: WorkflowVerificationResult,
) -> WorkflowExport:
    require_verified_workflow(draft, verification)
    root = ET.Element("mxfile", host="app.diagrams.net", version="24.7.17", compressed="false")
    diagram = ET.SubElement(root, "diagram", id=draft.workflow_id, name="Approved workflow")
    model = ET.SubElement(diagram, "mxGraphModel", dx="1200", dy="800", grid="1", gridSize="10", page="1")
    graph = ET.SubElement(model, "root")
    ET.SubElement(graph, "mxCell", id="0")
    ET.SubElement(graph, "mxCell", id="1", parent="0")
    actor_index = {actor.actor_id: index for index, actor in enumerate(draft.actors)}
    actor_label = {actor.actor_id: actor.label for actor in draft.actors}
    for index, node in enumerate(draft.nodes):
        evidence = ", ".join(node.supporting_evidence_ids)
        value = node.label + (f"&#xa;Evidence: {evidence}" if evidence else "")
        cell = ET.SubElement(
            graph,
            "mxCell",
            id=node.node_id,
            value=value,
            style=_drawio_style(node, _ACTOR_COLORS[actor_index[node.actor_id] % len(_ACTOR_COLORS)]),
            vertex="1",
            parent="1",
            actor=actor_label[node.actor_id],
            evidenceIds=",".join(node.supporting_evidence_ids),
            nodeType=node.node_type.value,
            changeType=node.change_type.value,
        )
        ET.SubElement(cell, "mxGeometry", x=str(80 + (index % 3) * 340), y=str(80 + (index // 3) * 150), width="260", height="80", **{"as": "geometry"})
    for edge in draft.edges:
        cell = ET.SubElement(
            graph,
            "mxCell",
            id=edge.edge_id,
            value=edge.condition or "",
            style="edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;endArrow=block;endFill=1;strokeColor=#52657A;",
            edge="1",
            parent="1",
            source=edge.source_id,
            target=edge.target_id,
            evidenceIds=",".join(edge.evidence_ids),
        )
        ET.SubElement(cell, "mxGeometry", relative="1", **{"as": "geometry"})
    ET.indent(root, space="  ")
    content = ET.tostring(root, encoding="unicode", short_empty_elements=True) + "\n"
    # Parsing here makes malformed escaping impossible to return silently.
    ET.fromstring(content)
    validate_drawio_xml(content, expected_pages=1)
    return WorkflowExport(content=content, sha256=_hash(content))


def _drawio_page(parent: ET.Element, draft: WorkflowDraft, page_name: str) -> None:
    diagram = ET.SubElement(parent, "diagram", id=draft.workflow_id, name=page_name)
    model = ET.SubElement(
        diagram, "mxGraphModel", dx="1800", dy="1000", grid="1", gridSize="10",
        page="1", pageScale="1", pageWidth="2200", pageHeight="1200",
    )
    graph = ET.SubElement(model, "root")
    ET.SubElement(graph, "mxCell", id=f"{draft.workflow_id}-0")
    root_id = f"{draft.workflow_id}-1"
    ET.SubElement(graph, "mxCell", id=root_id, parent=f"{draft.workflow_id}-0")
    actor_order = {actor.actor_id: index for index, actor in enumerate(draft.actors)}
    lane_width = max(2100, 260 * max(8, len(draft.nodes) // 2))
    for actor in draft.actors:
        lane = ET.SubElement(
            graph, "mxCell", id=f"{draft.workflow_id}-{actor.actor_id}", value=actor.label,
            style="swimlane;horizontal=0;startSize=34;fillColor=#0F172A;swimlaneFillColor=#111827;strokeColor=#475569;fontColor=#F8FAFC;fontStyle=1;",
            vertex="1", parent=root_id,
        )
        ET.SubElement(
            lane, "mxGeometry", x="20", y=str(20 + actor_order[actor.actor_id] * 220),
            width=str(lane_width), height="200", **{"as": "geometry"},
        )
    positions = {actor.actor_id: 0 for actor in draft.actors}
    for node in draft.nodes:
        index = positions[node.actor_id]
        positions[node.actor_id] += 1
        evidence = ", ".join(node.supporting_evidence_ids)
        cell = ET.SubElement(
            graph, "mxCell", id=f"{draft.workflow_id}-{node.node_id}", value=node.label,
            style=_drawio_style(node, "#172554").replace("fontColor=#152536", "fontColor=#F8FAFC"), vertex="1",
            parent=f"{draft.workflow_id}-{node.actor_id}", evidenceIds=evidence,
            nodeType=node.node_type.value, changeType=node.change_type.value,
        )
        ET.SubElement(
            cell, "mxGeometry", x=str(70 + index * 250), y="55", width="205", height="86",
            **{"as": "geometry"},
        )
    for edge in draft.edges:
        cell = ET.SubElement(
            graph, "mxCell", id=f"{draft.workflow_id}-{edge.edge_id}", value=edge.condition or "",
            style="edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;endArrow=block;endFill=1;strokeColor=#64748B;fontColor=#E2E8F0;",
            edge="1", parent=root_id,
            source=f"{draft.workflow_id}-{edge.source_id}", target=f"{draft.workflow_id}-{edge.target_id}",
            evidenceIds=",".join(edge.evidence_ids),
        )
        ET.SubElement(cell, "mxGeometry", relative="1", **{"as": "geometry"})


def export_drawio_bundle(
    original: WorkflowDraft,
    original_verification: WorkflowVerificationResult,
    updated: WorkflowDraft,
    updated_verification: WorkflowVerificationResult,
) -> WorkflowExport:
    """Export two landscape, editable swimlane pages for Lucidchart import."""

    require_verified_workflow(original, original_verification)
    require_verified_workflow(updated, updated_verification)
    root = ET.Element("mxfile", host="app.diagrams.net", version="24.7.17", compressed="false")
    _drawio_page(root, original, "Original Approved Workflow")
    _drawio_page(root, updated, "Updated Approved Workflow")
    ET.indent(root, space="  ")
    content = ET.tostring(root, encoding="unicode", short_empty_elements=True) + "\n"
    ET.fromstring(content)
    validate_drawio_xml(content, expected_pages=2)
    return WorkflowExport(content=content, sha256=_hash(content))


def _legacy_mermaid_preview_html(mermaid_source: str) -> str:
    """Return a controlled horizontal preview; rendering occurs only in the UI."""

    safe_source = html.escape(mermaid_source)
    return (
        '<style>html,body{margin:0;background:#080d17;color:#e2e8f0;font-family:Inter,system-ui,sans-serif}'
        '#workflow-shell{background:#080d17;border:1px solid #26344b;border-radius:14px;overflow:hidden}'
        '#workflow-tools{display:flex;gap:8px;align-items:center;padding:10px 12px;background:#0d1626;border-bottom:1px solid #26344b;position:sticky;left:0}'
        '#workflow-tools button{background:#172554;color:#e0f2fe;border:1px solid #2563eb;border-radius:7px;padding:7px 12px;font-weight:700;cursor:pointer}'
        '#workflow-tools span{color:#94a3b8;font-size:13px;margin-left:auto}'
        '#workflow-scroll{overflow-x:auto;overflow-y:auto;min-height:560px;padding:20px;background-image:radial-gradient(#263247 1px,transparent 1px);background-size:20px 20px}'
        '#workflow-preview{width:max-content;min-width:100%;transform-origin:top left}'
        '#workflow-preview svg{max-width:none!important;height:auto}button:focus{outline:2px solid #67e8f9;outline-offset:2px}</style>'
        '<div id="workflow-shell"><div id="workflow-tools"><button id="fit">Fit to width</button>'
        '<button id="minus" aria-label="Zoom out">−</button><button id="plus" aria-label="Zoom in">+</button>'
        '<button id="full">Fullscreen</button><span>Scroll horizontally to inspect the full process</span></div>'
        '<div id="workflow-scroll"><div id="workflow-preview" class="mermaid">'
        f"{safe_source}</div></div></div>"
        '<script type="module">import mermaid from '
        "'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs';"
        "mermaid.initialize({startOnLoad:false,securityLevel:'strict',theme:'dark',"
        "flowchart:{curve:'basis',htmlLabels:true},themeVariables:{primaryColor:'#172554',primaryTextColor:'#f8fafc',secondaryColor:'#0f2f3f',tertiaryColor:'#111827',lineColor:'#94a3b8',clusterBkg:'#0d1626',clusterBorder:'#475569'}});"
        "let scale=1;const preview=document.getElementById('workflow-preview');const scroll=document.getElementById('workflow-scroll');"
        "function apply(){const svg=preview.querySelector('svg');if(svg){svg.style.transform='scale('+scale+')';svg.style.transformOrigin='top left';preview.style.height=(svg.getBoundingClientRect().height+24)+'px';}}"
        "document.getElementById('plus').onclick=()=>{scale=Math.min(2,scale+.15);apply()};"
        "document.getElementById('minus').onclick=()=>{scale=Math.max(.35,scale-.15);apply()};"
        "document.getElementById('fit').onclick=()=>{const svg=preview.querySelector('svg');if(svg){const width=svg.viewBox.baseVal.width||svg.getBoundingClientRect().width;scale=Math.min(1,(scroll.clientWidth-42)/width);apply();scroll.scrollLeft=0}};"
        "document.getElementById('full').onclick=()=>document.getElementById('workflow-shell').requestFullscreen?.();"
        "mermaid.run({querySelector:'#workflow-preview'}).then(()=>document.getElementById('fit').click()).catch(()=>{preview.innerHTML="
        "'<div style=\"padding:24px;color:#cbd5e1;border:1px solid #475569;border-radius:10px\">' +"
        "'<b>Workflow preview unavailable.</b><br><span style=\"color:#94a3b8\">The verified Mermaid and Draw.io downloads remain available below.</span></div>';});</script>"
    )


def validate_drawio_xml(content: str, *, expected_pages: int | None = None) -> None:
    """Validate native editable Draw.io XML and reject disguised payloads."""

    if content.lstrip().startswith("%PDF"):
        raise ValueError("Draw.io export cannot contain a PDF payload")
    root = ET.fromstring(content)
    if root.tag != "mxfile":
        raise ValueError("Draw.io export must use an mxfile root")
    pages = root.findall("diagram")
    if expected_pages is not None and len(pages) != expected_pages:
        raise ValueError("Draw.io export page count does not match")
    for page in pages:
        cells = page.findall(".//mxCell")
        if not any(cell.attrib.get("vertex") == "1" for cell in cells):
            raise ValueError("Draw.io page contains no editable nodes")
        if not any(cell.attrib.get("edge") == "1" for cell in cells):
            raise ValueError("Draw.io page contains no editable connectors")


def mermaid_preview_html(mermaid_source: str) -> str:
    """Return a readable full-detail viewer that starts at actual size."""

    safe_source = html.escape(mermaid_source)
    return (
        '<style>html,body{margin:0;background:#12141A;color:#E8E6DE;font-family:system-ui,sans-serif}'
        '#workflow-shell{background:#12141A;border:1px solid #2A2E3A;border-radius:10px;overflow:hidden}'
        '#workflow-tools{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:10px 12px;background:#1B1E27;border-bottom:1px solid #2A2E3A;position:sticky;left:0}'
        '#workflow-tools button{background:#252934;color:#E8E6DE;border:1px solid #4B5060;border-radius:6px;padding:7px 12px;font-weight:700;cursor:pointer}'
        '#workflow-tools span{color:#9A9CA8;font-size:13px;margin-left:auto}'
        '#workflow-scroll{overflow:auto;min-height:560px;padding:20px;background-image:radial-gradient(#2A2E3A 1px,transparent 1px);background-size:20px 20px}'
        '#workflow-preview{width:max-content;min-width:100%;transform-origin:top left}'
        '#workflow-preview svg{max-width:none!important;height:auto}button:focus{outline:2px solid #C98A3E;outline-offset:2px}</style>'
        '<div id="workflow-shell"><div id="workflow-tools"><button id="fit">Fit readable area</button>'
        '<button id="actual">Actual size</button><button id="minus" aria-label="Zoom out">−</button>'
        '<button id="plus" aria-label="Zoom in">+</button><button id="full">Fullscreen</button>'
        '<span>Drag or scroll horizontally to inspect the complete process.</span></div>'
        '<div id="workflow-scroll"><div id="workflow-preview" class="mermaid">'
        f"{safe_source}</div></div></div>"
        '<script type="module">import mermaid from '
        "'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs';"
        "mermaid.initialize({startOnLoad:false,securityLevel:'strict',theme:'dark',"
        "flowchart:{curve:'basis',htmlLabels:true},themeVariables:{primaryColor:'#252934',primaryTextColor:'#E8E6DE',secondaryColor:'#24342A',tertiaryColor:'#1B1E27',lineColor:'#9A9CA8',clusterBkg:'#1B1E27',clusterBorder:'#4B5060'}});"
        "let scale=1;const preview=document.getElementById('workflow-preview');const scroll=document.getElementById('workflow-scroll');"
        "function apply(){const svg=preview.querySelector('svg');if(svg){svg.style.transform='scale('+scale+')';svg.style.transformOrigin='top left';preview.style.height=(svg.getBoundingClientRect().height+24)+'px';}}"
        "document.getElementById('plus').onclick=()=>{scale=Math.min(2,scale+.1);apply()};"
        "document.getElementById('minus').onclick=()=>{scale=Math.max(.72,scale-.1);apply()};"
        "document.getElementById('actual').onclick=()=>{scale=1;apply();scroll.scrollLeft=0};"
        "document.getElementById('fit').onclick=()=>{const svg=preview.querySelector('svg');if(svg){const width=svg.viewBox.baseVal.width||svg.getBoundingClientRect().width;scale=Math.max(.72,Math.min(1,(scroll.clientWidth-42)/width));apply();scroll.scrollLeft=0}};"
        "document.getElementById('full').onclick=()=>document.getElementById('workflow-shell').requestFullscreen?.();"
        "mermaid.run({querySelector:'#workflow-preview'}).then(()=>document.getElementById('actual').click()).catch(()=>{preview.innerHTML='<div style=\"padding:24px;color:#C9C7BE\"><b>Workflow preview unavailable.</b><br>The verified downloads remain available below.</div>';});</script>"
    )


def workflow_overview_html(
    draft: WorkflowDraft, verification: WorkflowVerificationResult
) -> str:
    """Render a readable presentation projection of the full verified workflow."""

    require_verified_workflow(draft, verification)
    actor_labels = {actor.actor_id: actor.label for actor in draft.actors}
    buckets: list[list[WorkflowNode]] = [[] for _ in range(6)]
    for index, node in enumerate(draft.nodes):
        bucket = min(5, (index * 6) // max(1, len(draft.nodes)))
        if node.node_type in {WorkflowNodeType.DECISION, WorkflowNodeType.START, WorkflowNodeType.END}:
            buckets[bucket].append(node)
        elif len(buckets[bucket]) < 2:
            buckets[bucket].append(node)
    titles = ("Begin", "Submit", "Validate", "Review", "Notify & update", "Complete")
    stage_cards: list[str] = []
    for index, (title, nodes) in enumerate(zip(titles, buckets)):
        rows = []
        for node in nodes[:3]:
            decision = " decision" if node.node_type == WorkflowNodeType.DECISION else ""
            changed = " changed" if node.change_type in {WorkflowChangeType.ADDED, WorkflowChangeType.MODIFIED} else ""
            rows.append(
                f'<div class="lane-row{decision}{changed}"><span>{html.escape(actor_labels[node.actor_id])}</span>'
                f'<b>{html.escape(node.label)}</b></div>'
            )
        arrow = '<span class="stage-arrow" aria-hidden="true">→</span>' if index < 5 else ""
        stage_cards.append(
            f'<section class="stage"><h3>{index + 1}. {title}</h3>{"".join(rows)}{arrow}</section>'
        )
    actors = "".join(f"<span>{html.escape(actor.label)}</span>" for actor in draft.actors)
    return (
        '<style>html,body{margin:0;background:#12141A;color:#E8E6DE;font-family:system-ui,sans-serif}'
        '.shell{padding:18px;background-image:radial-gradient(#2A2E3A 1px,transparent 1px);background-size:20px 20px;overflow-x:auto}'
        '.actors{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}.actors span{border-left:3px solid #C98A3E;background:#1B1E27;padding:6px 10px;color:#C9C7BE;font-size:12px}'
        '.stages{display:grid;grid-template-columns:repeat(6,minmax(150px,1fr));gap:16px;min-width:1080px}'
        '.stage{position:relative;background:#1B1E27;border:1px solid #2A2E3A;border-radius:8px;padding:11px;min-height:210px}'
        '.stage h3{font-family:Georgia,serif;font-size:15px;margin:0 0 10px;color:#E8E6DE}'
        '.lane-row{background:#222630;border:1px solid #353A47;border-left:3px solid #7A7E8C;border-radius:6px;padding:8px;margin:7px 0}'
        '.lane-row span{display:block;color:#9A9CA8;font-size:10px;text-transform:uppercase}.lane-row b{display:block;font-size:12px;line-height:1.35;margin-top:3px}'
        '.lane-row.decision{border-color:#C98A3E}.lane-row.decision b:before{content:"◇ ";color:#C98A3E}'
        '.lane-row.changed{border-left-color:#5C8768}.stage-arrow{position:absolute;right:-14px;top:48%;color:#C98A3E;font-size:20px;z-index:2}'
        '.hint{color:#9A9CA8;font-size:12px;margin-top:12px}</style>'
        '<div class="shell" role="img" aria-label="Approved workflow overview">'
        f'<div class="actors">{actors}</div><div class="stages">{"".join(stage_cards)}</div>'
        '<div class="hint">Overview projection of the full verified workflow. Open Full detail for every node and connector.</div></div>'
    )
