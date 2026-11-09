from django.apps import AppConfig
from django.db.backends.signals import connection_created


def configure_sqlite_performance(sender, connection, **kwargs):
    if connection.vendor == 'sqlite':
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA journal_mode = WAL;")
            cursor.execute("PRAGMA synchronous = NORMAL;")
            cursor.execute("PRAGMA cache_size = -64000;")
            cursor.execute("PRAGMA mmap_size = 268435456;")
            cursor.execute("PRAGMA temp_store = MEMORY;")


class SchedulerConfig(AppConfig):
    name = 'scheduler'

    def ready(self):
        connection_created.connect(configure_sqlite_performance)
