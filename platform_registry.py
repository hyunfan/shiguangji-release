"""Platform behavior registry; native identities and legacy paths stay separate."""
from dataclasses import dataclass
from account_identity import canonical_handle as x_canonical_handle, snapshot as x_snapshot
from platform_accounts import collection_key, is_instagram, empty_identity, account_info


@dataclass(frozen=True)
class Account:
    platform: str
    username: str
    key: str
    user_id: str = ''

    @classmethod
    def from_key(cls, key, user_id=''):
        key = collection_key(key)
        info = account_info(key)
        return cls(info['platform'],info['display_username'],key,user_id)


@dataclass(frozen=True)
class Platform:
    name: str
    canonical: object
    identity: object


PLATFORMS = {
    'x':Platform('x',x_canonical_handle,x_snapshot),
    'instagram':Platform('instagram',lambda base,key:key,lambda base,key:empty_identity(key)),
}


def platform_for(key):
    return PLATFORMS['instagram' if is_instagram(key) else 'x']
