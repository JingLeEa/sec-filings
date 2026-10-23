import argparse
import json
from pathlib import Path

from synthesis_dev.agent import build_synthesis_graph


def main():
    parser = argparse.ArgumentParser(
        description="Run the Financial Synthesis Agent"
    )

    parser.add_argument(
        "--input",
        default="synthesis_dev/sample_input.json",
        help="Path to the synthesis input JSON",
    )

    parser.add_argument(
        "--output",
        default="synthesis_dev/synthesis_output.json",
        help="Path to save the result",
    )

    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    with input_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    graph = build_synthesis_graph()

    print("Running Financial Synthesis Agent...")

    result = graph.invoke({"request": data})

    output = {
        "company": data["company"],
        "years": data["years"],
        "financial_trends": result["trends"],
        "report": result["report"],
        "validation": result["validation"],
    }

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with output_path.open("w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    print("\n=== EXECUTIVE SUMMARY ===")
    print(result["report"]["executive_summary"])

    print("\n=== VALIDATION ===")
    print(json.dumps(result["validation"], indent=2))

    print(f"\nSaved to: {output_path}")


if __name__ == "__main__":
    main()
