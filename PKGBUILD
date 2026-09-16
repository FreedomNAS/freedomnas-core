# Maintainer: FreedomNAS Team <dev@freedomnas.io>
pkgname=freedomnas-core
pkgver=4.0.0
pkgrel=1
pkgdesc="FreedomNAS Core Engine — OS-agnostic storage appliance"
arch=('x86_64' 'aarch64')
url="https://codeberg.org/freedomnas/freedomnas-core"
license=('MPL-2.0')
depends=(
    'freedomnas-profiles'
    'python'
    'python-yaml'
    'python-cryptography'
)
makedepends=('python-build' 'python-installer' 'python-setuptools' 'python-wheel'
             'nim' 'git' 'gcc')
optdepends=(
    'zfs: ZFS support (highly recommended)'
    'zfs-dkms: ZFS kernel module on non-LTS kernels'
)
source=("git+https://codeberg.org/freedomnas/freedomnas-core.git#tag=v${pkgver}")
sha256sums=('SKIP')
install=freedomnas-core.install

build() {
    cd "${srcdir}/${pkgname}/nim"
    nimble -y --nimbleDir:"${srcdir}/nimble" native

    cd "${srcdir}/${pkgname}"
    python -m build --wheel --no-isolation
}

package() {
    cd "${srcdir}/${pkgname}"
    python -m installer --destdir="${pkgdir}" dist/*.whl

    # The OS profiles are their own package (freedomnas-profiles) and are
    # installed to /usr/lib/freedomnas/profiles by it, not duplicated here.

    [ -f LICENSE ] && install -Dm 644 LICENSE "${pkgdir}/usr/share/licenses/${pkgname}/LICENSE"
    install -Dm 644 README.md "${pkgdir}/usr/share/doc/${pkgname}/README.md"
}
