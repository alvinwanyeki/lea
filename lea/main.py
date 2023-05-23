from __future__ import annotations

import concurrent.futures
import datetime as dt
import functools
import getpass
import io
import json
import os
import pathlib
import pickle
import time

import dotenv
import rich.console
import rich.live
import rich.table
import typer

import lea.clients
import lea.dag
import lea.views

dotenv.load_dotenv()
app = typer.Typer()
console = rich.console.Console()


def _make_client(username):
    from google.oauth2 import service_account

    return lea.clients.BigQuery(
        credentials=service_account.Credentials.from_service_account_info(
            json.loads(os.environ["CARBONFACT_SERVICE_ACCOUNT"])
        ),
        project_id="carbonfact-gsheet",
        dataset_name=os.environ["SCHEMA"],
        username=username,
    )


def _do_nothing():
    """This is a dummy function for dry runs"""


SUCCESS = "[green]SUCCESS"
RUNNING = "[yellow]RUNNING"
ERROR = "[red]ERROR"
SKIPPED = "[blue]SKIPPED"


@app.command()
def run(
    views_dir: str,
    only: list[str] = typer.Option(None),
    dry: bool = False,
    fresh: bool = False,
    production: bool = False,
    threads: int = 8,
    show: int = 20,
    raise_exceptions: bool = False,
):
    # Massage CLI inputs
    views_dir = pathlib.Path(views_dir)
    only = [tuple(v.split(".")) for v in only] if only else None

    # Determine the username, who will be the author of this run
    username = None if production else os.environ.get("USER", getpass.getuser())

    # The client determines where the views will be written
    # TODO: move this to a config file
    client = _make_client(username)

    # List the relevant views
    views = lea.views.load_views(views_dir)
    views = [view for view in views if view.schema not in {"tests", "funcs"}]
    console.log(f"{len(views):,d} view(s) in total")

    # Organize the views into a directed acyclic graph
    dag = lea.dag.DAGOfViews(views)

    # Determine which views need to be run
    blacklist = set(dag.keys()).difference(only) if only else set()
    console.log(f"{len(views) - len(blacklist):,d} view(s) selected")

    # Remove orphan views
    for schema, table in client.list_existing():
        if (schema, table) in dag:
            continue
        console.log(f"Removing {schema}.{table}")
        if not dry:
            delete_view = lea.views.GenericSQLView(schema=schema, name=table, query="")
            client.delete(delete_view)
        console.log(f"Removed {schema}.{table}")

    def display_progress() -> rich.table.Table:
        table = rich.table.Table(box=None)
        table.add_column("#", header_style="italic")
        table.add_column("schema", header_style="italic")
        table.add_column("view", header_style="italic")
        table.add_column("status", header_style="italic")
        table.add_column("duration", header_style="italic")

        order_not_done = [node for node in order if node not in cache]
        for i, (schema, view_name) in list(enumerate(order_not_done, start=1))[-show:]:
            status = SUCCESS if (schema, view_name) in jobs_ended_at else RUNNING
            status = ERROR if (schema, view_name) in exceptions else status
            status = SKIPPED if (schema, view_name) in skipped else status
            duration = (
                (
                    jobs_ended_at.get((schema, view_name), dt.datetime.now())
                    - jobs_started_at[(schema, view_name)]
                )
                if (schema, view_name) in jobs_started_at
                else dt.timedelta(seconds=0)
            )
            rounded_seconds = round(duration.total_seconds(), 1)
            table.add_row(str(i), schema, view_name, status, f"{rounded_seconds}s")

        return table

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=threads)
    jobs = {}
    order = []
    jobs_started_at = {}
    jobs_ended_at = {}
    exceptions = {}
    skipped = set()
    cache_path = pathlib.Path(".cache.pkl")
    cache = (
        set()
        if fresh or not cache_path.exists()
        else pickle.loads(cache_path.read_bytes())
    )
    tic = time.time()

    console.log(f"{len(cache):,d} view(s) already done")

    with rich.live.Live(
        display_progress(), vertical_overflow="visible", refresh_per_second=6
    ) as live:
        dag.prepare()
        while dag.is_active():
            # We check if new views have been unlocked
            # If so, we submit a job to create them
            # We record the job in a dict, so that we can check when it's done
            for node in dag.get_ready():
                if (
                    # Some nodes in the graph are not part of the views,
                    # they're external dependencies which can be ignored
                    node not in dag
                    # Some nodes are blacklisted, so we skip them
                    or node in blacklist
                ):
                    dag.done(node)
                    continue

                order.append(node)

                # A node can only be computed if all its dependencies have been computed
                # If all the dependencies have not been computed succesfully, we skip the node
                if any(
                    dep in skipped or dep in exceptions
                    for dep in dag[node].dependencies
                ):
                    skipped.add(node)
                    dag.done(node)
                    continue

                jobs[node] = executor.submit(
                    _do_nothing
                    if dry or node in cache
                    else functools.partial(client.create, view=dag[node])
                )
                jobs_started_at[node] = dt.datetime.now()
            # We check if any jobs are done
            # When a job is done, we notify the DAG, which will unlock the next views
            for node in jobs_started_at:
                if node not in jobs_ended_at and jobs[node].done():
                    dag.done(node)
                    jobs_ended_at[node] = dt.datetime.now()
                    # Determine whether the job succeeded or not
                    if exception := jobs[node].exception():
                        exceptions[node] = exception
            live.update(display_progress())

    # Save the cache
    all_done = not exceptions and not skipped
    cache = (
        set()
        if all_done
        else cache
        | {node for node in order if node not in exceptions and node not in skipped}
    )
    if cache:
        cache_path.write_bytes(pickle.dumps(cache))
    else:
        cache_path.unlink(missing_ok=True)

    # Summary statistics
    console.log(f"Took {round(time.time() - tic)}s")
    summary = rich.table.Table()
    summary.add_column("status")
    summary.add_column("count")
    if n := len(jobs_ended_at) - len(exceptions):
        summary.add_row(SUCCESS, f"{n:,d}")
    if n := len(exceptions):
        summary.add_row(ERROR, f"{n:,d}")
    if n := len(skipped):
        summary.add_row(SKIPPED, f"{n:,d}")
    console.print(summary)

    # Summary of errors
    if exceptions:
        for (schema, view_name), exception in exceptions.items():
            console.print(f"{schema}.{view_name}", style="bold red")
            console.print(exception)

        if raise_exceptions:
            raise Exception("Some views failed to build")


@app.command()
def export(views_dir: str, threads: int = 8):
    """

    HACK: this is too bespoke for Carbonfact

    """

    # Massage CLI inputs
    views_dir = pathlib.Path(views_dir)

    # List the export views
    views = lea.views.load_views(views_dir)
    views = [view for view in views if view.schema == "export"]
    console.log(f"{len(views):,d} view(s) in total")

    # List the accounts for which to produce exports
    accounts = (
        pathlib.Path(views_dir / "export" / "accounts.txt").read_text().splitlines()
    )
    console.log(f"{len(accounts):,d} account(s) in total")

    production = True
    None if production else os.environ.get("USER", getpass.getuser())

    from google.oauth2 import service_account

    account_clients = {
        account: lea.clients.BigQuery(
            credentials=service_account.Credentials.from_service_account_info(
                json.loads(os.environ["CARBONFACT_SERVICE_ACCOUNT"])
            ),
            project_id="carbonfact-gsheet",
            dataset_name=f"export_{account.replace('-', '_')}",
            username=None,
        )
        for account in accounts
    }

    def export_view_for_account(view, account):
        account_export = lea.views.GenericSQLView(
            schema="",
            name=view.name,
            query=f"""
            SELECT * EXCEPT (account_slug)
            FROM (
                {view.query}
            )
            WHERE account_slug = '{account}'
            """,
        )
        account_clients[account].create(account_export)

    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as executor:
        jobs = {
            executor.submit(
                functools.partial(export_view_for_account, view=view, account=account)
            ): (view, account)
            for view in views
            for account in account_clients
        }
        for job in concurrent.futures.as_completed(jobs):
            view, account = jobs[job]
            if exc := job.exception():
                console.log(f"Failed exporting {view} for {account}", style="bold red")
                console.log(exc, style="bold magenta")
            else:
                console.log(f"Exported {view.name} for {account}", style="bold green")


@app.command()
def test(views_dir: str, threads: int = 8, production: bool = False):
    # Massage CLI inputs
    views_dir = pathlib.Path(views_dir)

    # List the test views
    views = lea.views.load_views(views_dir)
    tests = [view for view in views if view.schema == "tests"]
    console.log(f"Found {len(tests):,d} tests")

    # A client is necessary for running tests, because each test is a query
    username = None if production else os.environ.get("USER", getpass.getuser())
    client = _make_client(username)

    def test_and_delete(test):
        # Each test leaves behind a table, so we delete it afterwards, because tests should
        # have no side-effects.
        # TODO: there's probably a way to run tests without leaving behind tables
        conflicts = client.load(test)
        client.delete(test)
        return conflicts

    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as executor:
        jobs = {executor.submit(test_and_delete, test): test for test in tests}
        for job in concurrent.futures.as_completed(jobs):
            test = jobs[job]
            try:
                conflicts = job.result()
                console.log(
                    str(test), style="bold green" if conflicts.empty else "bold red"
                )
            except Exception:
                console.log(f"Failed running {test}", style="bold magenta")


@app.command()
def archive(views_dir: str, view: str):
    # Massage CLI inputs
    views_dir = pathlib.Path(views_dir)
    schema, view_name = tuple(view.split("."))

    from google.oauth2 import service_account

    client = lea.clients.BigQuery(
        credentials=service_account.Credentials.from_service_account_info(
            json.loads(os.environ["CARBONFACT_SERVICE_ACCOUNT"])
        ),
        project_id="carbonfact-gsheet",
        dataset_name="archive",
        username=None,
    )

    view = {(view.schema, view.name): view for view in lea.views.load_views(views_dir)}[
        schema, view_name
    ]

    today = dt.date.today()
    archive_view = lea.views.GenericSQLView(
        schema="",
        name=f"kaya__{view.schema}__{view.name}__{today.strftime('%Y_%m_%d')}",
        query=f"SELECT * FROM kaya.{view.schema}__{view.name}",  # HACK
    )
    client.create(archive_view)


@app.command()
def docs(views_dir: str, output_dir: str = "docs"):
    # Massage CLI inputs
    views_dir = pathlib.Path(views_dir)
    output_dir = pathlib.Path(output_dir)

    # List all the relevant views
    views = lea.views.load_views(views_dir)
    views = [view for view in views if view.schema not in {"tests", "funcs"}]
    console.log(f"Found {len(views):,d} views")

    # Organize the views into a directed acyclic graph
    dag = lea.dag.DAGOfViews(views)

    # A client is necessary for getting the top 5 rows of each view
    client = _make_client(None)

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
            content.write(f"# `{schema}`\n\n")

        # Write down table of contents
        content.write("## Table of contents\n\n")
        for view in sorted(dag.values(), key=lambda view: view.name):
            if view.schema != schema:
                continue
            content.write(f"- [`{view.name}`](#{view.name})\n")
        content.write("\n")

        # Write down the views
        content.write("## Views\n\n")
        for view in sorted(dag.values(), key=lambda view: view.name):
            if view.schema != schema:
                continue
            content.write(f"### `{view.name}`\n\n")
            if view.description:
                content.write(f"{view.description}\n\n")

            # HACK: this whole block is a hack
            console.log(f"Getting preview for {view.schema}.{view.name}")
            head_view = lea.views.GenericSQLView(
                schema="kaya",
                name=f"{view.schema}__{view.name}__head",
                query=f"SELECT * FROM kaya.{view.schema}__{view.name} LIMIT 5",  # HACK
            )
            head = client.load(head_view)
            head_md = head.to_markdown(index=False)
            content.write(head_md)
            content.write("\n\n")
            client.delete(head_view)

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
