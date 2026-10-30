"""Read-only media grouping from explicit asset IDs, never visual similarity.

Counts describe saved metadata, not a fresh disk integrity check or proof of
exhaustive recovery. Group within each post; retain all original records/files.
"""
import re
from urllib.parse import urlsplit

SCHEMA = 1


def address(value):
    if not isinstance(value, str):
        return None
    if re.match(r'^(?:video|pbs)\.twimg\.com/', value, re.I):
        value = 'https://' + value
    try:
        url = urlsplit(value)
        if url.scheme not in {'http', 'https'}:
            return None
        if url.hostname == 'web.archive.org':
            replay = re.fullmatch(r'/web/(?:\d{1,14}|\*)[a-z_]*/(https?://.+)', url.path, re.I)
            if not replay:
                return None
            url = urlsplit(replay[1])
        return url
    except ValueError:
        return None


def values(media):
    urls = media.get('urls')
    provenance = media.get('provenance')
    provenance = provenance if isinstance(provenance, dict) else {}
    return [media.get('key'), *(urls if isinstance(urls, list) else []),
            media.get('source_url'), provenance.get('source_url'), media.get('archive_url')]


def video_identity(media):
    identities = set()
    for value in values(media):
        url = address(value)
        if not url or url.hostname != 'video.twimg.com':
            continue
        match = re.match(r'^/(ext_tw_video|amplify_video)/(\d+)/', url.path)
        if match:
            identities.add('/'.join(match.groups()))
    return (next(iter(identities)) if len(identities) == 1 else '', len(identities) > 1)


def is_cover(media):
    if media.get('kind') != 'image':
        return False
    for value in values(media):
        url = address(value)
        if url and url.hostname == 'pbs.twimg.com' and re.match(
                r'^/(ext_tw_video_thumb|amplify_video_thumb|tweet_video_thumb)/', url.path):
            return True
    return False


def is_saved(media):
    path = media.get('path')
    return (media.get('status') == 'saved' and isinstance(path, str)
            and path.startswith('media/') and '\\' not in path
            and all(part not in {'', '.', '..'} for part in path.split('/')))


def file_identity(media):
    digest, size = media.get('sha256'), media.get('bytes')
    if isinstance(digest, str) and re.fullmatch(r'[a-fA-F0-9]{64}', digest) and type(size) is int and size > 0:
        return digest.lower(), size
    return None


def inventory(posts):
    result = dict(schema=SCHEMA, video_groups=0, video_saved=0, video_missing=0,
                  video_unidentified=0, video_identity_conflicts=0, variant_records=0,
                  groups_with_missing_variants=0, image_records=0, image_saved=0,
                  cover_records=0, cover_saved=0, duplicate_saved_records=0,
                  invalid_saved_records=0, records=0)
    # Duplicate files across posts remain attached to each post. This count is
    # diagnostic only; it never changes either ownership or download status.
    files = set()
    for post in posts:
        groups = {}
        for index, media in enumerate(post.get('media', [])):
            result['records'] += 1
            saved = is_saved(media)
            if media.get('status') == 'saved' and not saved:
                result['invalid_saved_records'] += 1
            if saved and (identity := file_identity(media)):
                if identity in files:
                    result['duplicate_saved_records'] += 1
                files.add(identity)
            if media.get('kind') in {'video', 'hls'}:
                identity, conflict = video_identity(media)
                result['video_identity_conflicts'] += int(conflict)
                result['video_unidentified'] += int(not identity)
                result['variant_records'] += 1
                group = groups.setdefault(('asset', identity) if identity else ('record', index), [])
                # An HLS playlist alone does not constitute a saved video file.
                group.append(saved and media.get('kind') == 'video')
            elif media.get('kind') == 'image':
                prefix = 'cover' if is_cover(media) else 'image'
                result[prefix + '_records'] += 1
                result[prefix + '_saved'] += int(saved)
        for saved in groups.values():
            result['video_groups'] += 1
            result['video_saved'] += int(any(saved))
            result['video_missing'] += int(not any(saved))
            result['groups_with_missing_variants'] += int(any(saved) and not all(saved))
    return result
