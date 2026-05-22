# Maintainer: FreedomNAS Team <dev@freedomnas.io>
pkgname=freedomnas-core
pkgver=4.0.0
pkgrel=1
pkgdesc="FreedomNAS Core Engine — OS-agnostic storage appliance"
arch=('any')
url="https://codeberg.org/freedomnas/freedomnas-core"
license=('MPL-2.0')
depends=('python' 'python-pip' 'zfs')
makedepends=('python-setuptools')
optdepends=(
    'zfs: ZFS support (highly recommended)'
    'zfs-dkms: ZFS kernel module on non-LTS kernels'
)
source=("git+https://codeberg.org/freedomnas/freedomnas-core.git#tag=v${pkgver}")
sha256sums=('SKIP')
install=freedomnas-core.install

build() {
    cd "${srcdir}/${pkgname}"
    python setup.py build
}

package() {
    cd "${srcdir}/${pkgname}"
    python setup.py install --root="${pkgdir}/" --optimize=1 --skip-build

    # Install profiles to /usr/lib/freedomnas/profiles
    mkdir -p "${pkgdir}/usr/lib/freedomnas"
    cp -r ../freedomnas-profiles "${pkgdir}/usr/lib/freedomnas/profiles"

    # Install license
    install -Dm 644 LICENSE "${pkgdir}/usr/share/licenses/${pkgname}/LICENSE"
}
