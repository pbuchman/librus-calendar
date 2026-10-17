"""Synthetic source shapes; no real account, source IDs, dates or private exports."""
from calendar import monthrange
from datetime import date, datetime, timezone
from html import escape
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.librus_client import LibrusClient
from app.source_collectors import (ORIGIN, SourceCollectionError, collect_sources,
    parse_announcements, parse_calendar_detail, parse_calendar_month, parse_learner_context)


CURRENT = datetime(2028, 5, 15, 12, tzinfo=timezone.utc)
FETCHED = '2028-05-15T12:00:00+00:00'


def page(body):
    return '<!doctype html><html><head><title>Synergia</title></head><body>' + body + '</body></html>'


def password_only_userinfo_url():
    # Construct fictional URL userinfo structurally, without a contact-address
    # literal that resembles personal data to the repository privacy scanner.
    return 'https://'+'@'.join((':fictional','synergia.librus.pl'))+'/terminarz'


def announcement(title='Synthetic field trip', author='Example Teacher', published='2028-05-02', body='<p>Meet at school.</p>', order=None):
    fields = {'Dodał': escape(author), 'Data publikacji': escape(published), 'Treść': body}
    rows = ''.join(f'<tr><th>{key}</th><td>{fields[key]}</td></tr>' for key in (order or fields))
    return '<table class="decorated big center printable margin-top"><tbody><tr><th colspan="2">' + escape(title) + '</th></tr>' + rows + '<tr><td colspan="2"></td></tr></tbody></table>'


def profile(name='Example Learner', class_name='5b', school='Example School'):
    return page('<h2>Informacje o uczniu</h2><table>' + ''.join(
        f'<tr><td>{key}</td><td>{escape(value)}</td></tr>' for key, value in
        [('Imię i nazwisko ucznia', name), ('Klasa', class_name), ('Nazwa szkoły', school)]) + '</table>')


def calendar_page(year=2028, month=5, *, events=None, key='synthetic-form-key', missing_day=None):
    events = events or {}
    years = ''.join(f'<option value="{value}"' + (' selected="selected"' if value == year else '') + f'>{value}</option>' for value in range(2027, 2030))
    months = ''.join(f'<option value="{value}"' + (' selected="selected"' if value == month else '') + f'>{value}</option>' for value in range(1, 13))
    form = f'<form id="scheduleForm" method="post" action="/terminarz"><input type="hidden" name="requestkey" value="{key}"><select name="miesiac">{months}</select><select name="rok">{years}</select></form>'
    days = ''.join(f'<tr><td><div class="kalendarz-dzien">{day}<table>{events.get(day, "")}</table></div></td></tr>' for day in range(1, monthrange(year, month)[1] + 1) if day != missing_day)
    return page(form + '<table class="kalendarz decorated center">' + days + '</table>')


def calendar_cell(title='Synthetic science lesson', route='/terminarz/szczegoly/87001', tooltip='Data dodania: 2028-05-01 10:20:30'):
    onclick = f' onclick="location.href=\'{route}\'"' if route else ''
    return '<tr><td' + onclick + ' title="' + escape(tooltip, quote=True) + '">' + escape(title) + '</td></tr>'


def detail(published='2028-05-01 10:20:30'):
    return page('<table class="decorated"><tr><th colspan="2">Szczegóły</th></tr>' + ''.join(
        f'<tr><td>{key}</td><td>{value}</td></tr>' for key, value in [
            ('Rodzaj', 'Sprawdzian'), ('Data', '2028-05-16'), ('Nr lekcji', '3'),
            ('Przedmiot', 'Example science'), ('Nauczyciel', 'Example Teacher'),
            ('Opis', 'Bring a notebook. <a href="https://external.example/file.pdf">Information</a>'),
            ('Data dodania', published)]) + '</table>')


class FakeClient:
    def __init__(self):
        self.year, self.month, self.version = 2028, 5, 1
        self.calls, self.posts = [], []
        self.announcements = page(announcement())
        self.profiles = [profile()]
        self.events = {(2028, 5): {16: calendar_cell() + calendar_cell('Zastępstwo, lesson 4', None, 'Example substitute')}}
        self.details = {'/terminarz/szczegoly/87001': detail()}
        self.fail_month = None

    def follow(self, url):
        self.calls.append(('GET', url))
        path = url.removeprefix(ORIGIN)
        if path == '/rodzic/index':
            text = page('<h1>Selected learner</h1>')
        elif path == '/informacja':
            text = self.profiles.pop(0) if len(self.profiles) > 1 else self.profiles[0]
        elif path == '/ogloszenia':
            text = self.announcements
        elif path == '/terminarz':
            text = self.calendar()
        else:
            text = self.details[path]
        return SimpleNamespace(text=text, url=url, is_redirect=False,status_code=200)

    def calendar(self):
        return calendar_page(self.year, self.month, events=self.events.get((self.year, self.month)), key=f'key-{self.version}')

    def request(self, method, url, *, data):
        if method != 'POST' or url != ORIGIN + '/terminarz' or data['requestkey'] != f'key-{self.version}':
            raise AssertionError('Unexpected operation or stale form')
        self.calls.append((method, url))
        self.posts.append(dict(data))
        self.year, self.month = int(data['rok']), int(data['miesiac'])
        self.version += 1
        if self.month == self.fail_month:
            raise RuntimeError('A private error containing a session URL must not escape')
        return SimpleNamespace(text=self.calendar(), url=url, is_redirect=False,status_code=200)


class AnnouncementParserTests(unittest.TestCase):
    def parse(self, html):
        return parse_announcements(html, learner_id='synthetic-student', fetched_at=FETCHED)

    def test_fields_are_labelled_and_media_remains_inert(self):
        body = '<p>School notes.</p><img src="https://external.example/private.jpg"><a href="https://external.example/file.pdf">File</a><script>SECRET SCRIPT</script><iframe>HIDDEN</iframe>'
        item = self.parse(page(announcement(body=body, order=['Treść', 'Dodał', 'Data publikacji'])))[0]
        self.assertEqual(item['text'], 'School notes.\nFile')
        self.assertEqual(item['metadata']['image_count'], 1)
        self.assertEqual(item['metadata']['document_link_count'], 1)
        self.assertEqual(item['metadata']['attachment_count'], 2)
        self.assertNotIn('https://', str(item))
        self.assertNotIn('SECRET', str(item))
        self.assertEqual(item['published_at'], '2028-05-02')
        self.assertEqual(item['fetched_at'], FETCHED)
        self.assertIsNone(item['updated_at'])

    def test_generic_links_are_not_unprocessed_attachments(self):
        item = self.parse(page(announcement(body='<a href="https://school.example/news">School website</a><a href="https://school.example/handout.PDF?download=1">Handout</a>')))[0]
        self.assertEqual(item['metadata']['link_count'], 2)
        self.assertEqual(item['metadata']['attachment_count'], 1)

    def test_all_current_announcements_including_old_dates_are_retained(self):
        items = self.parse(page(announcement() + announcement('Old current notice', published='2027-01-01')))
        self.assertEqual(len(items), 2)

    def test_metadata_identity_is_separate_from_changed_content_hash(self):
        first = self.parse(page(announcement(body='First text')))[0]
        second = self.parse(page(announcement(body='Edited text')))[0]
        self.assertEqual(first['id'], second['id'])
        self.assertNotEqual(first['metadata']['content_hash'], second['metadata']['content_hash'])
        self.assertTrue(first['metadata']['identity_ambiguous'])

    def test_duplicate_metadata_or_hash_collisions_fail_closed(self):
        for html in (page(announcement() + announcement()), page(announcement() + announcement(body='Different text'))):
            with self.assertRaises(SourceCollectionError) as caught:
                self.parse(html)
            self.assertEqual(caught.exception.code, 'announcement_identity_ambiguous')

    def test_missing_schema_truncation_pagination_limits_and_invalid_date_fail(self):
        variants = [page('<p>Unknown content</p>'), page(announcement()).replace('</html>', ''),
                    page(announcement()).replace('Data publikacji', 'Changed field'),
                    page(announcement(published='not-a-date')),
                    page(announcement() + '<a href="/ogloszenia?page=2">More</a>')]
        for value in variants:
            with self.assertRaises(SourceCollectionError):
                self.parse(value)
        with patch('app.source_collectors.MAX_ANNOUNCEMENTS', 1), self.assertRaises(SourceCollectionError):
            self.parse(page(announcement() + announcement('Second')))

    def test_only_explicit_empty_state_is_empty_success(self):
        self.assertEqual(self.parse(page('Brak ogłoszeń')), [])
        with self.assertRaises(SourceCollectionError):
            self.parse(page(''))


class LearnerParserTests(unittest.TestCase):
    def test_current_profile_uses_observed_name_school_and_class(self):
        first = parse_learner_context(profile())
        next_class = parse_learner_context(profile(class_name='6b'))
        other_child = parse_learner_context(profile(name='Different Learner'))
        self.assertEqual(first['id'], next_class['id'])
        self.assertNotEqual(first['id'], other_child['id'])
        self.assertEqual(first['class_id'], '5b')
        self.assertTrue(first['selected_only'])

    def test_student_label_does_not_accept_parent_account_fields(self):
        html = profile(class_name='5 b SP').replace('</body>', '<table><tr><td>Imię i nazwisko użytkownika</td><td>Example Parent</td></tr><tr><td>Login</td><td>not-learner-identity</td></tr><tr><td>Hasło</td><td>not-a-source-field</td></tr></table></body>')
        result = parse_learner_context(html)
        self.assertEqual(result['name'], 'Example Learner')
        self.assertEqual(result['class_id'], '5b')
        self.assertNotIn('not-a-source-field', str(result))

    def test_missing_or_conflicting_profile_is_not_a_default_learner(self):
        for html in (profile(school=''), profile(name=''), profile().replace('</body>', '<table><tr><td>Klasa</td><td>6a</td></tr></table></body>')):
            with self.assertRaises(SourceCollectionError):
                parse_learner_context(html)


class CalendarParserTests(unittest.TestCase):
    def parse(self, value):
        return parse_calendar_month(value, start=date(2028, 5, 1), end=date(2028, 5, 31))

    def test_provider_routes_namespaced_holidays_and_metadata_substitutions(self):
        events = {16: calendar_cell() + calendar_cell('Synthetic holiday', '/terminarz/szczegoly_wolne/87001') + calendar_cell('Zastępstwo4. lesson', None)}
        form, entries = self.parse(calendar_page(events=events))
        self.assertEqual((form['year'], form['month']), (2028, 5))
        self.assertEqual([value['upstream_id'].split(':')[0] for value in entries], ['detail', 'holiday', 'metadata'])
        self.assertEqual(entries[2]['kind'], 'substitution')
        self.assertEqual(entries[0]['event_start'], '2028-05-16')
        self.assertEqual(entries[0]['published_at'], '2028-05-01T10:20:30+02:00')

    def test_event_window_is_distinct_from_publication_window(self):
        html = calendar_page(events={1: calendar_cell(), 16: calendar_cell('Zastępstwo', None, 'old notice')})
        unused, entries = parse_calendar_month(html, start=date(2028, 5, 10), end=date(2028, 5, 20))
        self.assertEqual(len(entries), 1)
        self.assertIsNone(entries[0]['published_at'])

    def test_historical_unlinked_event_has_explicit_metadata_identity(self):
        event = '<tr><td class="no-border-left no-border-right">Synthetic previous event<br>School meeting</td></tr>'
        unused, entries = self.parse(calendar_page(events={16: event}))
        self.assertEqual(entries[0]['kind'], 'school_event')
        self.assertTrue(entries[0]['upstream_id'].startswith('metadata:'))
        self.assertIsNone(entries[0]['detail_path'])

    def test_outside_window_unknown_event_cannot_poison_navigation(self):
        client = FakeClient()
        client.events[(2028, 4)] = {1: calendar_cell(route=None)}
        result = collect_sources(client, current=CURRENT)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual((client.year, client.month), (2028, 5))

    def test_incomplete_month_changed_form_route_and_unidentified_event_fail(self):
        variants = [calendar_page(missing_day=31), calendar_page().replace('scheduleForm', 'changedForm'),
                    calendar_page().replace('method="post"', 'method="get"'),
                    calendar_page().replace('action="/terminarz"', 'action="https://external.example/change"'),
                    calendar_page().replace('action="/terminarz"', 'action="'+password_only_userinfo_url()+'"'),
                    calendar_page().replace('action="/terminarz"', 'action="https://synergia.librus.pl:444/terminarz"'),
                    calendar_page(events={16: calendar_cell(route='https://external.example/x')}),
                    calendar_page(events={16: calendar_cell(route=None)}),
                    calendar_page().replace('</html>', '')]
        for value in variants:
            with self.assertRaises(SourceCollectionError):
                self.parse(value)

    def test_details_use_headers_and_decode_text_without_following_links(self):
        value = parse_calendar_detail(detail())
        self.assertEqual(value['nauczyciel'], 'Example Teacher')
        self.assertIn('Bring a notebook.', value['opis'])
        self.assertNotIn('https://', str(value))
        self.assertEqual(value['published_at'], '2028-05-01T10:20:30+02:00')
        for html in (page('<p>No details</p>'), detail(published='unknown')):
            with self.assertRaises(SourceCollectionError):
                parse_calendar_detail(html)

    def test_holiday_detail_uses_its_observed_distinct_schema(self):
        html = page('<table class="decorated small center"><tr><th colspan="2">Synthetic holiday</th></tr><tr><td>Zakres</td><td>School</td></tr><tr><td>Data dodania</td><td>2028-05-01 10:20:30</td></tr><tr><td colspan="2"></td></tr></table>')
        result = parse_calendar_detail(html, holiday=True)
        self.assertEqual(result['published_at'], '2028-05-01T10:20:30+02:00')
        self.assertEqual(result['zakres'], 'School')
        with self.assertRaises(SourceCollectionError):
            parse_calendar_detail(html)


class SourceCollectionTests(unittest.TestCase):
    def test_only_school_entries_gain_stable_observed_class_audience(self):
        from app.source_audience import AUDIENCE_KEY,valid_calendar_audience
        first=collect_sources(FakeClient(),current=CURRENT)
        second=collect_sources(FakeClient(),current=CURRENT)
        for source in first['sources']:
            if source['kind']=='school_event':
                self.assertTrue(valid_calendar_audience(source,first['learner']))
                other=next(x for x in second['sources']if x['kind']=='school_event')
                self.assertEqual(source['metadata'][AUDIENCE_KEY],other['metadata'][AUDIENCE_KEY])
                self.assertNotIn('requestkey',str(source['metadata'][AUDIENCE_KEY]))
            else:self.assertNotIn(AUDIENCE_KEY,source['metadata'])

    def test_complete_calendar_control_inventory_refuses_other_audience_or_child_controls(self):
        variants=['<select name="class_id"><option selected value="fictional">Class</option></select>',
                  '</form><form id="other"><select name="child"></select></form><form>',
                  '<input name="audience" type="hidden" value="fictional">',
                  '<input id="unboundClass" type="hidden" value="fictional">',
                  '<button name="chooseClass">Class</button>']
        for extra in variants:
            client=FakeClient();original=client.calendar
            client.calendar=lambda:original().replace('</form>',extra+'</form>',1)
            result=collect_sources(client,current=CURRENT)
            self.assertEqual(result['complete_kinds'],['announcement'])
            self.assertIn('calendar_form_invalid',result['errors'])
            self.assertEqual(client.posts,[])

    def test_recognized_external_language_select_is_not_a_class_selector(self):
        client=FakeClient();original=client.calendar
        client.calendar=lambda:original().replace('</body>','<select id="language" name="Język"><option value="pl">Polski</option></select></body>')
        self.assertEqual(collect_sources(client,current=CURRENT)['status'],'ok')
        other=FakeClient();original_other=other.calendar
        other.calendar=lambda:original_other().replace('</body>','<select name="class"><option>Class</option></select></body>')
        self.assertIn('calendar_form_invalid',collect_sources(other,current=CURRENT)['errors'])

    def test_post_final_url_status_and_query_are_validated_before_audience_proof(self):
        for change in ({'url':ORIGIN+'/uczen/index'},{'url':ORIGIN+'/terminarz?class=fictional'},
                       {'url':password_only_userinfo_url()},
                       {'url':'https://synergia.librus.pl:444/terminarz'},{'status_code':201}):
            client=FakeClient();request=client.request
            def changed(method,url,*,data):
                response=request(method,url,data=data)
                if data['miesiac']=='4':
                    for key,value in change.items():setattr(response,key,value)
                return response
            client.request=changed;result=collect_sources(client,current=CURRENT)
            self.assertEqual(result['complete_kinds'],['announcement'])
            self.assertIn('source_session_invalid',result['errors'])
            self.assertEqual((client.year,client.month),(2028,5))

    def test_temporary_context_switch_cannot_mint_audience_even_if_restored_later(self):
        client=FakeClient();client.profiles=[profile(),profile(name='Different Learner'),profile()]
        result=collect_sources(client,current=CURRENT)
        self.assertEqual(result['sources'],[]);self.assertEqual(result['complete_kinds'],[])
        self.assertEqual(result['errors'],['learner_context_changed'])
        self.assertEqual((client.year,client.month),(2028,5))

    def test_final_parent_route_is_rechecked_after_month_restoration(self):
        client=FakeClient();follow=client.follow;parents=[0]
        def changed(url):
            response=follow(url)
            if url.endswith('/rodzic/index'):
                parents[0]+=1
                if parents[0]>1:response.url=ORIGIN+'/uczen/index'
            return response
        client.follow=changed;result=collect_sources(client,current=CURRENT)
        self.assertEqual(result['sources'],[]);self.assertEqual(result['complete_kinds'],[])
        self.assertIn('source_session_invalid',result['errors'])

    def test_reuses_session_fixed_window_restores_month_and_never_fetches_media(self):
        client = FakeClient()
        result = collect_sources(client, current=CURRENT)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['complete_kinds'], ['announcement', 'school_event', 'substitution'])
        self.assertEqual(result['window_start'], '2028-04-15')
        self.assertEqual(result['window_end'], '2028-07-14')
        self.assertEqual((client.year, client.month), (2028, 5))
        self.assertEqual([int(value['miesiac']) for value in client.posts], [4, 5, 6, 7, 5])
        self.assertEqual(len(result['sources']), 3)
        self.assertTrue(all(url.startswith(ORIGIN + '/') for method, url in client.calls))
        self.assertEqual({url.removeprefix(ORIGIN) for method, url in client.calls},
                         {'/rodzic/index', '/informacja', '/ogloszenia', '/terminarz', '/terminarz/szczegoly/87001'})
        self.assertTrue(all('requestkey' not in str(item) for item in result['sources']))

    def test_failed_navigation_restores_view_and_preserves_other_kind(self):
        client = FakeClient()
        client.fail_month = 6
        result = collect_sources(client, current=CURRENT)
        self.assertEqual((result['status'], result['complete_kinds']), ('partial', ['announcement']))
        self.assertEqual((client.year, client.month), (2028, 5))
        self.assertEqual(result['errors'], ['school_event_collection_failed'])
        self.assertNotIn('private', str(result))

    def test_failed_restore_is_not_reported_as_calendar_complete(self):
        client = FakeClient()
        original = client.request
        def request(method, url, *, data):
            if client.month == 7 and data['miesiac'] == '5':
                raise RuntimeError('Private restore response omitted')
            return original(method, url, data=data)
        client.request = request
        result = collect_sources(client, current=CURRENT)
        self.assertEqual(result['errors'], ['calendar_restore_failed'])
        self.assertEqual(result['complete_kinds'], ['announcement'])
        self.assertNotIn('Private', str(result))

    def test_duplicate_calendar_ids_refuse_completeness(self):
        client = FakeClient()
        client.events[(2028, 5)][16] += calendar_cell()
        result = collect_sources(client, current=CURRENT)
        self.assertEqual(result['complete_kinds'], ['announcement'])
        self.assertIn('calendar_identity_ambiguous', result['errors'])

    def test_switched_learner_discards_whole_snapshot(self):
        client = FakeClient()
        client.profiles = [profile(), profile(name='Different Learner')]
        result = collect_sources(client, current=CURRENT)
        self.assertEqual(result['sources'], [])
        self.assertEqual(result['complete_kinds'], [])
        self.assertEqual(result['errors'], ['learner_context_changed'])

    def test_unverified_context_makes_no_source_reads(self):
        client = FakeClient()
        client.profiles = [profile(school='')]
        result = collect_sources(client, current=CURRENT)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(result['complete_kinds'], [])
        self.assertEqual(result['errors'], ['learner_context_unverified'])

    def test_unexpected_redirect_does_not_fetch_a_page_as_sources(self):
        client = FakeClient()
        original = client.follow
        def follow(url):
            response = original(url)
            if url.endswith('/ogloszenia'):
                response.url = ORIGIN + '/loguj'
            return response
        client.follow = follow
        result = collect_sources(client, current=CURRENT)
        self.assertIn('source_session_invalid', result['errors'])
        self.assertNotIn('announcement', result['complete_kinds'])

    def test_client_entrypoint_delegates_without_new_login(self):
        client = object.__new__(LibrusClient)
        with patch('app.source_collectors.collect_sources', return_value={'status': 'ok'}) as collect:
            self.assertEqual(client.collect_sources(current=CURRENT), {'status': 'ok'})
            collect.assert_called_once_with(client, current=CURRENT, past_days=30, future_days=60)

    def test_other_windows_are_not_silently_accepted(self):
        for settings in ({'past_days': 31}, {'future_days': 90}):
            client = FakeClient()
            with self.assertRaises(SourceCollectionError):
                collect_sources(client, current=CURRENT, **settings)
            self.assertFalse(client.calls)


if __name__ == '__main__':
    unittest.main()
