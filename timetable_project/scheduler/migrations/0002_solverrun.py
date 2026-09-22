from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('scheduler', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='SolverRun',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('cpu_percent_before', models.FloatField()),
                ('cpu_percent_after', models.FloatField()),
                ('ram_used_before_mb', models.FloatField()),
                ('ram_used_after_mb', models.FloatField()),
                ('wall_time_seconds', models.FloatField()),
                ('variable_count', models.PositiveIntegerField()),
                ('constraint_count', models.PositiveIntegerField()),
                ('solver_status', models.CharField(max_length=32)),
                ('semester', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='solver_runs', to='scheduler.semester')),
            ],
            options={'ordering': ['-created_at']},
        ),
    ]