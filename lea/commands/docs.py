from __future__ import annotations

import io
import pathlib

import rich.console

import lea


def docs(
    views_dir: str,
    output_dir: str,
    client: lea.clients.Client,
    console: rich.console.Console,
):
    views_dir = pathlib.Path(views_dir)
    output_dir = pathlib.Path(output_dir)

    # List all the relevant views
    views = lea.views.load_views(views_dir)
    views = [view for view in views if view.schema not in {"tests", "funcs"}]
    console.log(f"Found {len(views):,d} views")

    # Organize the views into a directed acyclic graph
    dag = lea.dag.DAGOfViews(views)

    # List all the columns
    columns = client.get_columns()

    # Now we can generate the docs for each schema and view therein
    readme_content = io.StringIO()
    readme_content.write("# Views\n\n")
    readme_content.write("## Schemas\n\n")
    for schema in dag.schemas:
        readme_content.write(f"- [`{schema}`](./{schema})\n")
        content = io.StringIO()

        # Write down the schema description if it exists
        if (existing_readme := views_dir / schema / "README.md").exists():
            content.write(existing_readme.read_text() + "\n")
        else:
            content.write(f"# {schema}\n\n")

        # Write down table of contents
        content.write("## Table of contents\n\n")
        for view in sorted(dag.values(), key=lambda view: view.name):
            if view.schema != schema:
                continue
            content.write(f"- [{view.name}](#{view.name})\n")
        content.write("\n")

        # Write down the views
        content.write("## Views\n\n")
        for view in sorted(dag.values(), key=lambda view: view.name):
            if view.schema != schema:
                continue
            content.write(f"### {view.name}\n\n")
            if view.description:
                content.write(f"{view.description}\n\n")

            # Write down the query
            # HACK
            content.write(
                "```sql\n"
                "SELECT *\n"
                f"FROM {client.dataset_name}.{schema}__{view.name}\n"
                "```\n\n"
            )
            # Write down the columns
            view_columns = columns.query(f"table == '{schema}__{view.name}'")[
                ["column", "type"]
            ]
            view_comments = view.extract_comments(
                columns=view_columns["column"].tolist(), dialect=client.sqlglot_dialect
            )
            view_columns["Description"] = (
                view_columns["column"]
                .map(
                    {
                        column: " ".join(
                            comment.text
                            for comment in comment_block
                            if not comment.text.startswith("@")
                        )
                        for column, comment_block in view_comments.items()
                    }
                )
                .fillna("")
            )
            view_columns["Unique"] = (
                view_columns["column"]
                .map(
                    {
                        column: "✅"
                        if any(comment.text == "@UNIQUE" for comment in comment_block)
                        else ""
                        for column, comment_block in view_comments.items()
                    }
                )
                .fillna("")
            )
            view_columns["type"] = view_columns["type"].apply(lambda x: f"`{x}`")
            view_columns = view_columns.rename(
                columns={"column": "Column", "type": "Type"}
            )
            view_columns = view_columns.sort_values("Column")
            content.write(view_columns.to_markdown(index=False) + "\n\n")

        # Write the schema README
        schema_readme = output_dir / schema / "README.md"
        schema_readme.parent.mkdir(parents=True, exist_ok=True)
        schema_readme.write_text(content.getvalue())
        console.log(f"Wrote {schema_readme}", style="bold green")
    else:
        readme_content.write("\n")

    # Flowchart
    mermaid = dag.to_mermaid()
    mermaid = mermaid.replace("style", "style_")  # HACK
    readme_content.write("## Flowchart\n\n")
    readme_content.write(f"```mermaid\n{mermaid}```\n")

    # Write the root README
    readme = output_dir / "README.md"
    readme.parent.mkdir(parents=True, exist_ok=True)
    readme.write_text(readme_content.getvalue())