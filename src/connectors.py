import os


def read_cosmos(query, side):
    """Run a read-only query against the Source or Target Cosmos DB container.

    side is 'SOURCE' or 'TARGET'; the connection comes from the
    <side>_COSMOS_ENDPOINT, _KEY, _DATABASE and _CONTAINER environment variables.
    """
    from azure.cosmos import CosmosClient
    prefix = f'{side}_COSMOS_'
    client = CosmosClient(os.environ[prefix + 'ENDPOINT'], credential=os.environ[prefix + 'KEY'])
    container = client.get_database_client(os.environ[prefix + 'DATABASE']).get_container_client(os.environ[prefix + 'CONTAINER'])
    return list(container.query_items(query=query, enable_cross_partition_query=True))
