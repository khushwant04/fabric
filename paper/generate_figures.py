"""Generate every figure embedded in the Fabric technical paper.

The charts intentionally visualize only values in data/evidence.json. Architecture
annotations come from the deployed Helm manifests and dated deployment record.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Rectangle

ROOT = Path(__file__).resolve().parent
DATA = json.loads((ROOT / "data" / "evidence.json").read_text())
OUT = ROOT / "figures"
OUT.mkdir(parents=True, exist_ok=True)

NAVY = "#16324F"
BLUE = "#2878B5"
CYAN = "#55A6C8"
GREEN = "#2E8B57"
ORANGE = "#E07A3F"
PURPLE = "#7451A6"
RED = "#C84C4C"
GRAY = "#6B7280"
LIGHT = "#F4F7FA"
INK = "#17202A"

plt.rcParams.update(
    {
        "font.family": "DejaVu Sans",
        "font.size": 9,
        "axes.titlesize": 12,
        "axes.labelsize": 9,
        "figure.titlesize": 14,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "pdf.fonttype": 42,
        "ps.fonttype": 42,
    }
)


def save(fig: Figure, name: str) -> None:
    """Write one vector PDF and one review PNG with stable PDF metadata."""
    fig.savefig(
        OUT / f"{name}.pdf",
        bbox_inches="tight",
        metadata={
            "Creator": "Fabric paper figure generator",
            "CreationDate": None,
            "ModDate": None,
        },
    )
    fig.savefig(OUT / f"{name}.png", dpi=220, bbox_inches="tight")
    plt.close(fig)


def box(
    ax: Axes,
    xy: tuple[float, float],
    width: float,
    height: float,
    title: str,
    detail: str = "",
    *,
    face: str = "white",
    edge: str = NAVY,
    title_size: float = 8.5,
) -> None:
    x, y = xy
    patch = FancyBboxPatch(
        (x, y),
        width,
        height,
        boxstyle="round,pad=0.012,rounding_size=0.012",
        facecolor=face,
        edgecolor=edge,
        linewidth=1.25,
    )
    ax.add_patch(patch)
    ax.text(x + width / 2, y + height * 0.63, title, ha="center", va="center", weight="bold", fontsize=title_size, color=INK)
    if detail:
        ax.text(x + width / 2, y + height * 0.28, detail, ha="center", va="center", fontsize=7.2, color=GRAY, linespacing=1.12)


def arrow(
    ax: Axes,
    start: tuple[float, float],
    end: tuple[float, float],
    label: str = "",
    *,
    color: str = BLUE,
    style: str = "-|>",
    curve: float = 0.0,
    dashed: bool = False,
    label_offset: tuple[float, float] = (0.0, 0.0),
) -> None:
    patch = FancyArrowPatch(
        start,
        end,
        arrowstyle=style,
        mutation_scale=10,
        linewidth=1.25,
        color=color,
        connectionstyle=f"arc3,rad={curve}",
        linestyle="--" if dashed else "-",
        zorder=3,
    )
    ax.add_patch(patch)
    if label:
        mx = (start[0] + end[0]) / 2 + label_offset[0]
        my = (start[1] + end[1]) / 2 + label_offset[1]
        ax.text(mx, my, label, ha="center", va="center", fontsize=6.7, color=color, backgroundcolor="white", zorder=4)


def cloud_architecture() -> None:
    snapshot = DATA["deployment_snapshot"]
    cloudflare = snapshot["cloudflare"]
    fig, ax = plt.subplots(figsize=(14.2, 8.2))
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    # Region and namespace boundaries.
    ax.add_patch(Rectangle((0.19, 0.06), 0.79, 0.88, facecolor="#FBFCFE", edgecolor="#9CB3C9", linewidth=1.5))
    ax.text(
        0.205,
        0.92,
        f"{snapshot['region']} · {snapshot['resource_group']}",
        fontsize=10.5,
        weight="bold",
        color=NAVY,
    )
    ax.add_patch(Rectangle((0.39, 0.12), 0.56, 0.70, facecolor="#F8FAFC", edgecolor="#B7C5D3", linewidth=1.0))
    ax.text(
        0.405,
        0.79,
        f"AKS {snapshot['kubernetes_version']} · Azure CNI / Cilium · Entra RBAC",
        fontsize=9,
        weight="bold",
        color=NAVY,
    )
    ax.add_patch(Rectangle((0.42, 0.51), 0.23, 0.23, facecolor="#EEF6FC", edgecolor=BLUE, linewidth=1.0))
    ax.text(0.43, 0.715, "fabric-control", fontsize=8, weight="bold", color=BLUE)
    ax.add_patch(Rectangle((0.67, 0.22), 0.25, 0.52, facecolor="#F3F8F4", edgecolor=GREEN, linewidth=1.0))
    ax.text(0.68, 0.715, "fabric-stamp", fontsize=8, weight="bold", color=GREEN)
    ax.add_patch(Rectangle((0.42, 0.17), 0.23, 0.27, facecolor="#F8F4FB", edgecolor=PURPLE, linewidth=1.0))
    ax.text(0.43, 0.415, "fabric-observability", fontsize=8, weight="bold", color=PURPLE)

    # Public edge.
    box(ax, (0.015, 0.63), 0.13, 0.12, "API / OpenAI clients", "control + inference", face="#FFFFFF")
    box(
        ax,
        (0.015, 0.38),
        0.13,
        0.14,
        "Cloudflare DNS",
        f"control + Grafana: {cloudflare['control']}\ninference: {cloudflare['inference']}",
        face="#FFF7F0",
        edge=ORANGE,
    )
    box(
        ax,
        (0.22, 0.62),
        0.13,
        0.12,
        "Azure public IP",
        f"{snapshot['public_ip']}\nshared edge",
        face="#EFF6FF",
        edge=BLUE,
    )
    box(ax, (0.22, 0.43), 0.13, 0.13, "Istio ingress", "HTTPS + LE certs\nHTTP redirect", face="#EFF6FF", edge=BLUE)

    arrow(ax, (0.08, 0.63), (0.08, 0.52), "DNS / HTTPS", color=ORANGE, label_offset=(0.035, 0))
    arrow(ax, (0.145, 0.45), (0.22, 0.68), "direct inference HTTPS", color=GREEN, curve=0.14, label_offset=(0.03, 0.015))
    arrow(ax, (0.145, 0.45), (0.22, 0.50), "proxied HTTPS", color=ORANGE, curve=-0.08, label_offset=(0, -0.015))
    arrow(ax, (0.285, 0.62), (0.285, 0.56), color=BLUE)

    # Control plane.
    box(
        ax,
        (0.445, 0.58),
        0.18,
        0.095,
        f"Control plane ×{snapshot['control_plane_replicas']}",
        "FastAPI · token / intent / usage",
        face="white",
        edge=BLUE,
    )
    box(
        ax,
        (0.235, 0.18),
        0.14,
        0.11,
        f"PostgreSQL {snapshot['postgresql_version']}",
        f"RLS · {snapshot['postgresql_sku']} · {snapshot['postgresql_storage_gib']} GiB",
        face="#FFF7F0",
        edge=ORANGE,
    )
    box(ax, (0.235, 0.31), 0.14, 0.09, "Key Vault", "administrative secret source", face="#FFF7F0", edge=ORANGE)
    box(ax, (0.015, 0.16), 0.13, 0.09, "Auth0 / account OIDC", "human identity assertions", face="#FFF7F0", edge=ORANGE)
    arrow(ax, (0.35, 0.50), (0.445, 0.625), "VirtualService → HTTP", color=BLUE, curve=-0.12)
    arrow(ax, (0.445, 0.60), (0.375, 0.235), "TLS + app role", color=ORANGE, curve=0.08)
    arrow(ax, (0.145, 0.205), (0.445, 0.60), "JWKS / OIDC HTTPS", color=ORANGE, curve=-0.18)
    arrow(ax, (0.305, 0.31), (0.47, 0.58), "deploy-time secret copy", color=GRAY, dashed=True, curve=-0.12)

    # Stamp and model hosts.
    box(
        ax,
        (0.69, 0.60),
        0.20,
        0.085,
        "Agent · data plane · collector",
        "one StatefulSet · app roles split\nshared agent SA token (known gap)",
        face="white",
        edge=RED,
    )
    box(ax, (0.69, 0.49), 0.095, 0.075, "Operator", "Kubernetes RBAC", face="white", edge=GREEN)
    box(ax, (0.795, 0.49), 0.095, 0.075, "CR + routes", "desired / observed", face="white", edge=GREEN)
    box(
        ax,
        (0.69, 0.27),
        0.20,
        0.15,
        f"{snapshot['model_hosts']} vLLM model hosts",
        f"{snapshot['gpu_nodes']} × {snapshot['gpu_model']} · 1 GPU each\nheadless Services · /health",
        face="#ECF8F0",
        edge=GREEN,
    )
    arrow(ax, (0.35, 0.50), (0.69, 0.642), "VirtualService → data plane HTTP", color=GREEN, curve=0.08)
    arrow(ax, (0.69, 0.62), (0.625, 0.62), "outbound desired/status HTTPS", color=PURPLE, style="<|-|>", curve=0.12, label_offset=(0, 0.025))
    arrow(ax, (0.785, 0.527), (0.795, 0.527), "", color=PURPLE)
    arrow(ax, (0.74, 0.49), (0.79, 0.42), "reconcile", color=PURPLE, curve=0.1)
    arrow(ax, (0.79, 0.60), (0.79, 0.42), "local HTTP :8000", color=GREEN, label_offset=(0.052, 0))

    # Observability and ACR.
    box(ax, (0.445, 0.31), 0.18, 0.07, "Prometheus + Grafana", "gateway · engine · cluster", face="white", edge=PURPLE)
    box(
        ax,
        (0.445, 0.21),
        0.18,
        0.065,
        f"DCGM exporters ×{snapshot['dcgm_exporters']}",
        "GPU metrics / node",
        face="white",
        edge=PURPLE,
    )
    box(
        ax,
        (0.445, 0.08),
        0.18,
        0.065,
        "Azure Container Registry",
        f"{snapshot['acr_repositories']} image repositories",
        face="#F1F5F9",
        edge=GRAY,
    )
    arrow(ax, (0.69, 0.60), (0.625, 0.345), "scrape HTTP", color=PURPLE, curve=0.15)
    arrow(ax, (0.69, 0.32), (0.625, 0.245), "GPU telemetry", color=PURPLE, curve=-0.12)
    arrow(ax, (0.625, 0.112), (0.69, 0.30), "kubelet pull HTTPS", color=GRAY, dashed=True, curve=0.1)

    # Legend and explicit caveat.
    legend = [(BLUE, "public/control request"), (GREEN, "inference/local serving"), (PURPLE, "control/telemetry"), (GRAY, "supply/admin")]
    for idx, (color, label) in enumerate(legend):
        x = 0.21 + idx * 0.17
        ax.plot([x, x + 0.035], [0.025, 0.025], color=color, linewidth=2)
        ax.text(x + 0.04, 0.025, label, va="center", fontsize=7, color=GRAY)
    ax.text(0.98, 0.015, "Deployed service-to-service links are HTTP unless explicitly marked TLS.", ha="right", fontsize=7, color=RED)
    fig.suptitle("Fabric request, control, supply, and telemetry paths", x=0.5, y=0.985, weight="bold", color=INK)
    save(fig, "cloud_architecture")


def fleet_evolution() -> None:
    rows = DATA["fleet_snapshots"]
    labels = [row["label"] for row in rows]
    serving = [row["serving_replicas"] for row in rows]
    spare = [row["spare_nodes"] for row in rows]
    aliases = [row["model_aliases"] for row in rows]
    x = list(range(len(rows)))

    fig, ax = plt.subplots(figsize=(8.6, 4.5))
    ax.bar(x, serving, color=GREEN, width=0.58, label="serving replicas")
    ax.bar(x, spare, bottom=serving, color="#C9D3DD", width=0.58, label="unallocated GPU nodes")
    for idx, total in enumerate([a + b for a, b in zip(serving, spare, strict=True)]):
        ax.text(idx, total + 0.18, f"{total} GPUs", ha="center", weight="bold", color=NAVY)
        ax.text(idx, serving[idx] / 2, str(serving[idx]), ha="center", va="center", color="white", weight="bold", fontsize=11)
    ax.set_xticks(x, labels)
    ax.set_ylabel("T4 nodes / model replicas")
    ax.set_ylim(0, 9.5)
    ax.grid(axis="y", alpha=0.2)
    ax2 = ax.twinx()
    ax2.plot(x, aliases, marker="o", color=ORANGE, linewidth=2, label="model aliases")
    ax2.set_ylabel("distinct model aliases", color=ORANGE)
    ax2.set_ylim(0, 6.3)
    ax2.tick_params(axis="y", colors=ORANGE)
    lines, names = ax.get_legend_handles_labels()
    lines2, names2 = ax2.get_legend_handles_labels()
    ax.legend(lines + lines2, names + names2, loc="upper center", ncol=3, frameon=False)
    ax.set_title("Fleet snapshots are discrete audits, not a continuous time series", weight="bold")
    fig.text(0.01, 0.01, "Sources: 18 Sep deployment record and September live audits. The post-scale fleet trades rollout spare for lower standing cost.", fontsize=7.5, color=GRAY)
    save(fig, "fleet_evolution")


def current_model_inventory() -> None:
    models = DATA["current_models"]
    labels = [item["alias"] for item in models]
    weights = [item["weight_gb"] for item in models]
    y = list(range(len(models)))
    colors = [CYAN, BLUE, NAVY, PURPLE, ORANGE]

    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(10.5, 4.7), gridspec_kw={"width_ratios": [1.35, 1]})
    ax.barh(y, weights, color=colors)
    ax.set_yticks(y, labels)
    ax.invert_yaxis()
    ax.set_xlabel("nominal checkpoint weight size (GB)")
    ax.set_title("Model variety", weight="bold")
    ax.grid(axis="x", alpha=0.2)
    for idx, value in enumerate(weights):
        ax.text(value + 0.15, idx, f"{value:.1f}", va="center", fontsize=8)

    ax2.axis("off")
    ax2.set_title("20 Sep audit result", weight="bold")
    col_x = [0.02, 0.52, 0.76]
    headers = ["alias", "Ready", "chat"]
    for x, header in zip(col_x, headers, strict=True):
        ax2.text(x, 0.91, header, weight="bold", color=NAVY, transform=ax2.transAxes)
    for idx, item in enumerate(models):
        y_pos = 0.79 - idx * 0.15
        ax2.text(col_x[0], y_pos, item["alias"], fontsize=8, transform=ax2.transAxes)
        ax2.text(col_x[1], y_pos, f"{item['ready']}/{item['replicas']}", color=GREEN, weight="bold", transform=ax2.transAxes)
        ax2.text(col_x[2], y_pos, f"HTTP {item['http_status']}", color=GREEN, weight="bold", transform=ax2.transAxes)
    ax2.text(0.02, 0.03, "One small authenticated request per alias;\nnot a reliability or throughput sample.", fontsize=8, color=RED, transform=ax2.transAxes)
    fig.suptitle("Current five-model inventory and point-in-time verification", weight="bold")
    fig.text(0.01, 0.01, "Weight sizes are model-metadata facts, not observed VRAM consumption. Each model currently has one T4 replica.", fontsize=7.5, color=GRAY)
    save(fig, "current_model_inventory")


def admission_outcome() -> None:
    row = DATA["admission_burst"]
    fig, ax = plt.subplots(figsize=(8.2, 3.6))
    served = row["served"]
    rejected = row["rate_limited_429"]
    ax.barh([0], [served], color=GREEN, height=0.42, label="served")
    ax.barh([0], [rejected], left=[served], color=ORANGE, height=0.42, label="HTTP 429")
    ax.text(served / 2, 0, f"{served} served", color="white", ha="center", va="center", weight="bold")
    ax.text(served + rejected / 2, 0, f"{rejected} limited", color="white", ha="center", va="center", weight="bold")
    ax.set_xlim(0, row["offered_requests"])
    ax.set_yticks([])
    ax.set_xlabel("requests in one concurrent burst")
    ax.set_title("Admission remained explicit under overload", weight="bold")
    ax.legend(loc="lower center", bbox_to_anchor=(0.5, -0.43), ncol=2, frameon=False)
    ax.text(15, -0.32, f"Usage attribution observed for all {row['usage_attributed']} requests", ha="center", fontsize=8, color=PURPLE)
    fig.text(0.01, 0.01, "Earlier two-T4 pilot; one configured stamp-local concurrency cap. This is not a five-node throughput benchmark.", fontsize=7.5, color=GRAY)
    save(fig, "admission_outcome")


def kernel_crossover() -> None:
    row = DATA["kernel_microbenchmark"]
    sequences = row["sequences"]
    ratios = [base / fabric for base, fabric in zip(row["baseline_us"], row["fabric_us"], strict=True)]
    fig, ax = plt.subplots(figsize=(8.4, 4.4))
    ax.axhline(1.0, color=GRAY, linewidth=1.1, linestyle="--")
    ax.plot(sequences, ratios, marker="o", markersize=7, color=BLUE, linewidth=2)
    for x, ratio in zip(sequences, ratios, strict=True):
        ax.annotate(f"{ratio:.3f}×", (x, ratio), xytext=(0, 9 if ratio >= 1 else -16), textcoords="offset points", ha="center", weight="bold", color=NAVY)
    ax.set_xscale("log", base=2)
    ax.set_xticks(sequences, [str(value) for value in sequences])
    ax.set_xlabel("active sequences in microbenchmark")
    ax.set_ylabel("vLLM baseline time / Fabric time")
    ax.set_ylim(0.94, 1.23)
    ax.grid(alpha=0.2)
    ax.fill_between([0.8, 40], 1.0, 1.23, color=GREEN, alpha=0.07)
    ax.fill_between([0.8, 40], 0.94, 1.0, color=RED, alpha=0.06)
    ax.set_title("Development microkernel benefit crosses over toward parity", weight="bold")
    ax.text(1.1, 1.205, "Fabric faster", color=GREEN, fontsize=8)
    ax.text(17, 0.955, "baseline faster", color=RED, fontsize=8)
    fig.text(0.01, 0.01, f"{row['hardware']}; {row['warmups']} warmups, {row['repetitions']} repetitions; vLLM 0.11 unpacked operation. Development-only—current T4 fleet uses standard kernels.", fontsize=7.3, color=GRAY)
    save(fig, "kernel_crossover")


def cost_capacity() -> None:
    row = DATA["cost_scenarios"]
    nodes = row["gpu_nodes"]
    cost = row["hourly_usd"]
    fig, ax = plt.subplots(figsize=(8.5, 4.5))
    ax.plot(nodes, cost, marker="o", color=NAVY, linewidth=2.2)
    ax.scatter([5], [cost[nodes.index(5)]], s=120, color=ORANGE, zorder=3, label="current topology estimate")
    for x, y in zip(nodes, cost, strict=True):
        if x in {0, 5, 8}:
            ax.annotate(f"${y:.2f}/h", (x, y), xytext=(0, 10), textcoords="offset points", ha="center", fontsize=8, weight="bold")
    ax.set_xlabel("T4 GPU nodes")
    ax.set_ylabel("estimated USD per hour")
    ax.set_xticks(nodes)
    ax.set_ylim(0, 8.4)
    ax.grid(alpha=0.2)
    ax.legend(frameon=False)
    ax.set_title("Standing cost is dominated by GPU-node count", weight="bold")
    fig.text(0.01, 0.01, "Derived scenario, not invoices. Uses 18 Sep retail rates and a fixed non-GPU assumption; the current PostgreSQL SKU differs from that original bill of materials.", fontsize=7.3, color=GRAY)
    save(fig, "cost_capacity")


def main() -> None:
    cloud_architecture()
    fleet_evolution()
    current_model_inventory()
    admission_outcome()
    kernel_crossover()
    cost_capacity()
    print(f"Generated 6 PDF and 6 PNG figures in {OUT}")


if __name__ == "__main__":
    main()
