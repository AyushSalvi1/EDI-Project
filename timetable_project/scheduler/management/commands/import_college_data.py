import os
from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ValidationError
from scheduler.importer import import_college_data_from_json


class Command(BaseCommand):
    help = "Import college timetable data from a JSON file into the database."

    def add_arguments(self, parser):
        parser.add_argument("path_to_json", type=str, help="Path to the JSON file containing college data")

    def handle(self, *args, **options):
        json_path = options["path_to_json"]

        if not os.path.exists(json_path):
            raise CommandError(f"File does not exist at path: {json_path}")

        self.stdout.write(self.style.NOTICE(f"Validating and importing college data from: {json_path}..."))

        try:
            result = import_college_data_from_json(json_path)
            semester = result["semester"]
            self.stdout.write(self.style.SUCCESS(
                f"Successfully imported data for semester '{semester.name}'!\n"
                f"  - Created {result['divisions_count']} YearDivisions\n"
                f"  - Created {result['subjects_count']} Subjects\n"
                f"  - Created {result['rooms_count']} Rooms\n"
                f"  - Created {result['teachers_count']} Teachers\n"
                f"  - Created {result['assignments_count']} Assignments\n"
                f"  - Generated {result['slots_count']} TimeSlots"
            ))
        except ValidationError as ve:
            raise CommandError(f"Validation failed:\n{ve.message if hasattr(ve, 'message') else str(ve)}")
        except Exception as e:
            raise CommandError(f"Failed to import JSON data: {str(e)}")
