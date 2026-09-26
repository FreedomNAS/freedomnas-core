# Maintainer: FreedomNAS Team <dev@freedomnas.io>
pkgname=freedomnas-core
pkgver=1.0
pkgrel=1
pkgdesc="FreedomNAS Core Engine — OS-agnostic storage appliance"
arch=('any')
url="https://codeberg.org/freedomnas/freedomnas-core"
license=('MPL-2.0')
depends=(
    'freedomnas-profiles'
    'python'
    'python-yaml'
    'python-cryptography'
)
optdepends=(
    'zfs: ZFS support (highly recommended)'
    'zfs-dkms: ZFS kernel module on non-LTS kernels'
)
source=("git+https://codeberg.org/freedomnas/freedomnas-core.git#tag=v${pkgver}")
sha256sums=('SKIP')
install=freedomnas-core.install

# The core goes where every edition puts it: the package directory under
# /usr/lib/freedomnas/core, on sys.path through a .pth file. paths.py finds
# the profiles and the WebUI from there, which a wheel in site-packages
# could not.
package() {
    cd "${srcdir}/${pkgname}"
    local core_dir="${pkgdir}/usr/lib/freedomnas/core"
    local site_dir
    site_dir="$(python -c 'import sysconfig; print(sysconfig.get_path("purelib"))')"

    install -d "${core_dir}/freedomnas"
    cp -r *.py modules "${core_dir}/freedomnas/"
    find "${core_dir}" -name __pycache__ -prune -exec rm -rf {} +
    python -m compileall -q -s "${pkgdir}" -p / "${core_dir}"

    install -d "${pkgdir}${site_dir}"
    echo /usr/lib/freedomnas/core > "${pkgdir}${site_dir}/freedomnas-core.pth"
    install -Dm 755 bin/freedomnas "${pkgdir}/usr/bin/freedomnas"

    # The OS profiles are their own package (freedomnas-profiles) and are
    # installed to /usr/lib/freedomnas/profiles by it, not duplicated here.

    [ -f LICENSE ] && install -Dm 644 LICENSE "${pkgdir}/usr/share/licenses/${pkgname}/LICENSE"
    install -Dm 644 README.md "${pkgdir}/usr/share/doc/${pkgname}/README.md"
}
