"""Check literal numeric Unix cron schedules before Cloud Build deploys jobs."""
from pathlib import Path
import re

import pytest


ROOT=Path(__file__).resolve().parent.parent
SCHEDULE_ARGUMENT=re.compile(r'''--schedule(?:\s*=\s*|['"]\s*,\s*)(?:'([^']*)'|"([^"]*)"|([^\s'"\\]+))''')
FIELD_BOUNDS=((0,59),(0,23),(1,31),(1,12),(0,7))


def schedules(config):
    """Read quoted shell arguments and inline YAML argument arrays."""
    for match in SCHEDULE_ARGUMENT.finditer(config):
        value=next(group for group in match.groups() if group is not None)
        yield value,config.count('\n',0,match.start())+1


def is_runtime_schedule(value):
    # A shell/Cloud Build variable needs separate runtime validation; a literal
    # regression test must not pretend to have checked its resolved value.
    return '$' in value or '`' in value


def validate_literal_schedule(value):
    fields=value.split()
    assert len(fields)==5, f'Expected 5 cron fields, got {len(fields)}: {value!r}'
    for field,(lower,upper) in zip(fields,FIELD_BOUNDS):
        for item in field.split(','):
            base,slash,step=item.partition('/')
            if slash:assert step.isdecimal() and int(step)>0, f'Invalid cron step: {item!r}'
            if base=='*':continue
            match=re.fullmatch(r'(\d+)(?:-(\d+))?',base)
            assert match is not None, f'Invalid numeric cron field: {field!r}'
            start=int(match[1]);end=int(match[2] or match[1])
            assert lower<=start<=end<=upper, f'Cron field outside {lower}-{upper}: {field!r}'


def test_all_literal_cloudbuild_schedules_have_five_admissible_fields():
    found=list(schedules((ROOT/'cloudbuild.yaml').read_text()))
    assert found, 'No Cloud Build schedule arguments found'
    checked=0
    for value,line in found:
        if is_runtime_schedule(value):continue
        try:validate_literal_schedule(value)
        except AssertionError as error:raise AssertionError(f'cloudbuild.yaml:{line}: {error}') from error
        checked+=1
    assert checked, 'No literal schedules validated; runtime schedules require a separate check'


def test_extracts_shell_and_inline_array_schedules_and_recognizes_runtime_value():
    config="""--schedule='0 12 1 * *'
args: ['scheduler', 'jobs', '--schedule', '*/5 9-16 * * 1-5']
--schedule="$$CRON"
"""
    found=list(schedules(config))
    assert found==[('0 12 1 * *',1),('*/5 9-16 * * 1-5',2),('$$CRON',3)]
    assert not is_runtime_schedule(found[0][0])
    assert is_runtime_schedule(found[2][0])


@pytest.mark.parametrize('value',[
    '0 12 1 *', '60 12 * * *', '0 24 * * *', '0 12 0 * *',
    '0 12 * 13 *', '0 12 * * 8', '*/0 12 * * *', '0 15-10 * * *',
])
def test_literal_cron_rejects_missing_fields_and_invalid_ranges_or_steps(value):
    with pytest.raises(AssertionError):validate_literal_schedule(value)


def test_literal_cron_accepts_lists_ranges_and_positive_steps():
    validate_literal_schedule('5,35 10-15 1-7 * *')
    validate_literal_schedule('*/5 9-16 * * 1-5')
    validate_literal_schedule('0 12 * * 7')
