import uuid

from django.db import migrations, models


def populate_route_cache_ids(apps, schema_editor):
    route_cache = apps.get_model('geo', 'RouteCache')
    database = schema_editor.connection.alias
    batch = []
    for row in route_cache.objects.using(database).only('key').iterator(chunk_size=500):
        row.id = uuid.uuid4()
        batch.append(row)
        if len(batch) == 500:
            route_cache.objects.using(database).bulk_update(batch, ['id'])
            batch.clear()
    if batch:
        route_cache.objects.using(database).bulk_update(batch, ['id'])


class Migration(migrations.Migration):
    dependencies = [
        ('geo', '0002_routecache'),
    ]

    operations = [
        migrations.AddField(
            model_name='routecache',
            name='id',
            field=models.UUIDField(null=True, editable=False),
        ),
        migrations.RunPython(populate_route_cache_ids, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='routecache',
            name='key',
            field=models.CharField(max_length=64, unique=True),
        ),
        migrations.AlterField(
            model_name='routecache',
            name='id',
            field=models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False),
        ),
    ]
