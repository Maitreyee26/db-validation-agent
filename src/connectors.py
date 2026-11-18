import os


def read_sql(query):
    import pyodbc
    conn = pyodbc.connect(os.environ['SQL_CONNECTION_STRING'], timeout=15)
    try:
        cursor = conn.cursor()
        cursor.execute(query)
        columns = [c[0] for c in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        conn.close()


def read_cosmos(query):
    from azure.cosmos import CosmosClient
    client = CosmosClient(os.environ['COSMOS_ENDPOINT'], credential=os.environ['COSMOS_KEY'])
    container = client.get_database_client(os.environ['COSMOS_DATABASE']).get_container_client(os.environ['COSMOS_CONTAINER'])
    return list(container.query_items(query=query, enable_cross_partition_query=True))
