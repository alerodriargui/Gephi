import csv
from pathlib import Path

import networkx as nx
import requests
from bs4 import BeautifulSoup


SOURCE_URL = "https://www.merco.info/es/ranking-merco-empresas"
RANKING_CSV = Path("ranking_merco_empresas_espana_2025.csv")
OUTPUT_GEXF = Path("grafo_merco_empresas_espana_2025.gexf")


def load_companies():
    companies = {}
    with RANKING_CSV.open(encoding="utf-8-sig", newline="") as file:
        for row in csv.DictReader(file):
            companies[row["empresa"]] = {
                "posicion": int(row["posicion"]),
                "puntos": int(row["puntos"]),
                "posicion_anterior": (
                    int(row["posicion_anterior"])
                    if row["posicion_anterior"]
                    else 0
                ),
            }
    return companies


def load_sectors():
    response = requests.get(SOURCE_URL, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    sectors = {}
    for heading in soup.select("#ranking-sectorial h3.title-ranking"):
        table = heading.find_next_sibling("table")
        if table is None:
            continue

        sector = heading.get_text(" ", strip=True)
        members = []
        for row in table.select("tbody tr"):
            cells = row.find_all("td")
            if len(cells) == 2:
                members.append(
                    (int(cells[0].get_text(strip=True)), cells[1].get_text(" ", strip=True))
                )
        sectors[sector] = members

    if not sectors:
        raise RuntimeError("No se encontraron los rankings sectoriales")
    return sectors


def build_graph(companies, sectors):
    graph = nx.Graph(
        nombre="Ranking Merco Empresas Espana 2025",
        fuente=SOURCE_URL,
        tipo="bipartito empresa-sector",
    )

    for company, attributes in companies.items():
        graph.add_node(
            f"empresa:{company}",
            label=company,
            tipo="empresa",
            sector="",
            posicion_sector=0,
            **attributes,
        )

    for sector, members in sectors.items():
        sector_id = f"sector:{sector}"
        graph.add_node(
            sector_id,
            label=sector,
            tipo="sector",
            sector=sector,
            posicion=0,
            puntos=0,
            posicion_anterior=0,
            posicion_sector=0,
        )

        for sector_position, company in members:
            company_id = f"empresa:{company}"
            if company_id not in graph:
                raise RuntimeError(f"Empresa sectorial ausente del ranking global: {company}")

            graph.nodes[company_id]["sector"] = sector
            graph.nodes[company_id]["posicion_sector"] = sector_position
            graph.add_edge(
                company_id,
                sector_id,
                tipo="pertenece_a",
                peso=1.0,
                posicion_sector=sector_position,
            )

    return graph


def main():
    graph = build_graph(load_companies(), load_sectors())
    nx.write_gexf(graph, OUTPUT_GEXF, encoding="utf-8", prettyprint=True)
    print(
        f"{OUTPUT_GEXF}: {graph.number_of_nodes()} nodos, "
        f"{graph.number_of_edges()} aristas"
    )


if __name__ == "__main__":
    main()
