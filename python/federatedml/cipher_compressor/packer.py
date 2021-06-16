import functools
from federatedml.util import LOGGER
from federatedml.secureprotol.encrypt_mode import EncryptModeCalculator
from federatedml.cipher_compressor.compressor import PackingCipherTensor
from federatedml.cipher_compressor.compressor import CipherPackage
from federatedml.secureprotol.encrypt import IterativeAffineEncrypt, PaillierEncrypt
from federatedml.transfer_variable.transfer_class.cipher_compressor_transfer_variable \
    import CipherCompressorTransferVariable
from federatedml.util import consts


def get_homo_encryption_max_int(encrypter):

    if type(encrypter) == PaillierEncrypt:
        max_pos_int = encrypter.public_key.max_int
        min_neg_int = -max_pos_int
    elif type(encrypter) == IterativeAffineEncrypt:
        n_array = encrypter.key.n_array
        allowed_max_int = n_array[0]
        max_pos_int = int(allowed_max_int * 0.9) - 1  # the other 0.1 part is for negative num
        min_neg_int = (max_pos_int - allowed_max_int) + 1
    else:
        raise ValueError('unknown encryption type')

    return max_pos_int, min_neg_int


class GuestIntegerPacker(object):

    def __init__(self, pack_num: int, pack_num_range: list, encrypt_mode_calculator: EncryptModeCalculator,
                 need_cipher_compress=True):
        """
        max_int: max int allowed for packing result
        pack_num: number of int to pack, they must be POSITIVE integer
        pack_num_range: list of integer, it gives range of every integer to pack
        need_cipher_compress: if dont need cipher compress, related parameter will be set to 1
        """

        self._pack_num = pack_num
        assert len(pack_num_range) == self._pack_num, 'list len must equal to pack_num'
        self._pack_num_range = pack_num_range
        self._pack_num_bit = [i.bit_length() for i in pack_num_range]
        self.calculator = encrypt_mode_calculator

        max_pos_int, _ = get_homo_encryption_max_int(self.calculator.encrypter)
        self._max_int = max_pos_int
        self._max_bit = self._max_int.bit_length() - 1  # reserve 1 bit, in case overflow

        # sometimes max_int is not able to hold all num need to be packed, so we
        # use more than one large integer to pack them all
        self._bit_assignment = []
        self.total_bit_take = sum(self._pack_num_bit)
        tmp_list = []
        bit_count = 0
        for bit_len in self._pack_num_bit:
            if bit_count + bit_len >= self._max_bit:
                if bit_count == 0:
                    raise ValueError('unable to pack this num using in current int capacity')
                self._bit_assignment.append(tmp_list)
                tmp_list = []
                bit_count = 0
            bit_count += bit_len
            tmp_list.append(bit_len)

        if len(tmp_list) != 0:
            self._bit_assignment.append(tmp_list)
        self._pack_int_needed = len(self._bit_assignment)

        # transfer variable
        self.trans_var = CipherCompressorTransferVariable()
        if need_cipher_compress:
            compress_parameter = self.cipher_compress_suggest()
        else:
            compress_parameter = (None, 1)
        self.trans_var.compress_para.remote(compress_parameter, role=consts.HOST, idx=-1)

    def cipher_compress_suggest(self):
        if type(self.calculator.encrypter) == IterativeAffineEncrypt:  # iterativeAffine not support cipher compress
            return None, 1
        compressible = self._bit_assignment[-1]
        total_bit_count = sum(compressible)
        compress_num = self._max_bit // total_bit_count
        padding_bit = total_bit_count
        return padding_bit, compress_num

    def pack_int_list(self, int_list: list):

        assert len(int_list) == self._pack_num, 'list length is not equal to pack_num'
        start_idx = 0
        rs = []
        for bit_assign_of_one_int in self._bit_assignment:
            to_pack = int_list[start_idx: start_idx + len(bit_assign_of_one_int)]
            packing_rs = self._pack_fix_len_int_list(to_pack, bit_assign_of_one_int)
            rs.append(packing_rs)
            start_idx += len(bit_assign_of_one_int)

        return rs

    def _pack_fix_len_int_list(self, int_list: list, bit_assign: list):

        result = int_list[0]
        for i, offset in zip(int_list[1:], bit_assign[1:]):
            result = result << offset
            result += i

        return result


    def _unpack_an_int(self, integer: int, bit_assign_list: list):

        rs_list = []
        for bit_assign in reversed(bit_assign_list[1:]):
            mask_int = (2**bit_assign) - 1
            unpack_int = integer & mask_int
            rs_list.append(unpack_int)
            integer = integer >> bit_assign
        rs_list.append(integer)

        return list(reversed(rs_list))

    def _cipher_list_to_cipher_tensor(self, cipher_list: list):

        cipher_tensor = PackingCipherTensor(ciphers=cipher_list)
        return cipher_tensor

    def pack(self, data_table):
        packing_data_table = data_table.mapValues(self.pack_int_list)
        return packing_data_table

    def pack_and_encrypt(self, data_table, ret_cipher_tensor=True):

        packing_data_table = self.pack(data_table)
        en_packing_data_table = self.calculator.raw_encrypt(packing_data_table)
        if ret_cipher_tensor:
            en_packing_data_table = en_packing_data_table.mapValues(self._cipher_list_to_cipher_tensor)

        return en_packing_data_table

    def unpack_result(self, decrypted_result_list: list):

        final_rs = []
        for l_ in decrypted_result_list:

            assert len(l_) == len(self._bit_assignment), 'length of integer list is not equal to bit_assignment'

            rs_list = []
            for idx, integer in enumerate(l_):
                unpack_list = self._unpack_an_int(integer, self._bit_assignment[idx])
                rs_list.extend(unpack_list)

            final_rs.append(rs_list)

        return final_rs

    def _decrypt_cipher_packages(self, content):

        if type(content) == list:

            assert issubclass(type(content[0]), CipherPackage), 'content is not CipherPackages'
            decrypt_rs = []
            for i in content:
                unpack_ = i.unpack(self.calculator.encrypter, True)
                decrypt_rs += unpack_
            return decrypt_rs

        else:
            raise ValueError('illegal input type')

    def decrypt_cipher_package_and_unpack(self, data_table):

        de_func = functools.partial(self._decrypt_cipher_packages)
        de_table = data_table.mapValues(de_func)
        unpack_table = de_table.mapValues(self.unpack_result)

        return unpack_table


if __name__ == '__main__':

    from fate_arch.session import computing_session as session

    session.init('cwj', 0)

    en = IterativeAffineEncrypt()
    en.generate_key(1024)
    en_cal_mode = EncryptModeCalculator(encrypter=en)

    packer = GuestIntegerPacker(4, [1000000*2*2**53, 1000000*2*2**53, 1000000*2*2**53, 1000000*2*2**53], en_cal_mode)

    int_list_a = [412, 114514, 114514, 114514]
    int_list_b = [214, 1919, 1919, 1919]
    int_list_c = [1919810, 893, 893, 893]

    data_list = [int_list_a, int_list_b, int_list_c]
    dtable = session.parallelize(data_list, include_key=False, partition=4)

    en_table = packer.pack_and_encrypt(dtable)
    en_table = en_table.mapValues(lambda x: x*2)
    rs_table = packer.decrypt_cipher_and_unpack(en_table)

    en_rs = list(en_table.collect())
    compress_para = packer.cipher_compress_suggest()

    package = PackingCipherTensorPackage(compress_para[0], compress_para[1])
    package.add(en_rs[0][1])
    package.add(en_rs[1][1])
    package.add(en_rs[2][1])
    unpack_rs = package.unpack(en, True)
    for i in unpack_rs:
        print(i)
        print(packer.unpack_int_list(i))

    # unpack = package.unpack(en, True)
    # packing_rs = packer.pack_int_list([412, 123, 996, 114514])
    # cipher_tensor = packer._cipher_list_to_cipher_tensor(en.recursive_raw_encrypt(packing_rs))
    # cipher_tensor *= 2
    # cipher_tensor = cipher_tensor + cipher_tensor
    # cipher_tensor += 1
    # de_rs = packer._decrypt_a_cipher_content(cipher_tensor, en_cal_mode)
    # unpack_rs = packer.unpack_int_list(de_rs)

