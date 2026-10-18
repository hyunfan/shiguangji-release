"""Delete exactly one confirmed local collection while respecting its writer lock."""
import os
import re
import shutil
from pathlib import Path
from collection_lock import collection_lock
from platform_accounts import collection_key, is_instagram


def linked(path):
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())


def delete_collection(base, handle):
    if collection_key(handle) != handle:
        raise ValueError('无效的博主名。')
    root = Path(base).resolve(strict=True)
    folder = root / handle
    if linked(folder) or folder.resolve().parent != root:
        raise ValueError('合集目录是链接或超出保存目录，未删除。')
    archive = root / (handle + '-合集.zip')
    extras = [archive, archive.with_suffix('.zip.json'), archive.with_suffix('.zip.part'), archive.with_suffix('.zip.tmp')]
    for path in extras:
        if linked(path) or path.resolve().parent != root:
            raise ValueError('合集 ZIP 路径异常，未删除。')
    if not folder.exists() and not any(p.exists() for p in extras):
        return  # Idempotent: a lost response must not target a different account.
    with collection_lock(folder):
        # Validate the entire tree before deleting anything. Never follow a
        # junction/symlink into other user files, even beneath media/ or raw/.
        for directory, dirs, files in os.walk(folder, followlinks=False):
            for name in dirs + files:
                path = Path(directory) / name
                if linked(path) or not path.resolve().is_relative_to(folder):
                    raise ValueError('合集内含链接或越界路径，未删除，请先检查该目录。')
        for path in extras:
            path.unlink(missing_ok=True)
        children = sorted(folder.iterdir(), key=lambda p: p.name == 'manifest.json')
        for path in children:
            if path.name == '.collection.lock':
                continue
            # Recheck absolute targets immediately before recursive deletion.
            if linked(path) or not path.resolve().is_relative_to(folder):
                raise ValueError('文件路径发生变化，已停止删除。')
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        # The empty account directory keeps only its tiny lock file so another
        # process cannot recreate a second lock during deletion. No user data remains.

    # Identifier evidence is part of the deleted local collection, too.
    if not is_instagram(handle) and (root / '.account-identities.sqlite').exists():
        from account_identity import Catalog
        catalog = Catalog(root)
        try:
            with catalog.db:
                for table in ('observations', 'checked', 'choices'):
                    catalog.db.execute(f'DELETE FROM {table} WHERE collection=?', (handle,))
                catalog.db.execute('DELETE FROM settings WHERE key=?', ('alias_search:' + handle,))
        finally:
            catalog.close()
