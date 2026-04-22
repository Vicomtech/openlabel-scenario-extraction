import queries as q


def execute() -> None:
    q.handle_hard_brake_with_pedestrian_crossing()

    output_file = q.read_params.get("outputs", {}).get("queries_extras_file")
    if not output_file:
        print("[ERROR] Missing outputs.queries_extras_file in conf.yaml")
        return
    q.serialize_graph(output_file)


if __name__ == "__main__":
    execute()
